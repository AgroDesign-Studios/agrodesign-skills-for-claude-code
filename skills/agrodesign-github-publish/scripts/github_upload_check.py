#!/usr/bin/env python3
"""github_upload_check.py — stage, scan, bundle, gate and seal what goes to a public
place on GitHub; then, each only after the owner's recorded approval, push the checked
commit, read everything a switch to public would expose, switch, and release (the
agrodesign-github-publish skill).

  stage     --out RUN SOURCE...
  scan      --run RUN [--values-file FILE]
  bundle    --run RUN [--max-bytes N]
  gate      --run RUN --before codex|grok
  seal      --run RUN
  verify    --run RUN SOURCE...
  approve   --run RUN --step push|publish|release --repo OWNER/NAME --words TEXT
            [--valid-minutes N] [--release-tag TAG --release-target COMMIT
            --release-title FILE --release-notes FILE [--release-asset FILE]...]
  revoke    --run RUN --step push|publish|release --words TEXT
  push      --run RUN SOURCE...
  preflight --run RUN --repo OWNER/NAME
  publish   --run RUN --repo OWNER/NAME [--release-title FILE --release-notes FILE
            [--release-asset FILE]...]
  state     --run RUN --repo OWNER/NAME

SOURCE is any mix, given in the same order to stage, verify and push:
  --tree DIR                      a directory's files as they will appear (a `.git`
                                  inside is skipped; directory symlinks are recorded,
                                  not followed)
  --git REPO --push REMOTE REF [--whole-history]
                                  what `git push REMOTE REF` would send: every object
                                  the destination's refs (git ls-remote) do not
                                  already reach — each blob with its path, each raw
                                  commit (message, author, committer, signatures,
                                  merge tags) and an annotated tag's own object. A new
                                  or empty destination therefore stages the whole
                                  history; --whole-history stages it whatever the
                                  destination holds (required before a switch to
                                  public). What the destination held is recorded, and
                                  verify and push re-stage against that record, so a
                                  verify after the push still matches. The clone must
                                  hold every commit the destination has (fetch first).
  --file PATH                     one loose file: a release asset, the release title
                                  and the release notes (each written to a file)
  --repo-meta OWNER/NAME          the repository's description, homepage and topics
                                  (read with gh), public once the repository is

`stage` copies the set into RUN/tree (never under a home directory, which the Codex
stage's read-deny profile hides), makes it read-only, and writes RUN/manifest.json
and RUN/DIGEST; `scan`, `bundle`, `gate` and `seal` re-hash the tree first and refuse
a tree that changed. `scan` is the mechanical check before any AI reads the set
(gitleaks with archives and without allow comments, secret and
internal-information patterns, file names, LFS, the private values of the values
file — your own list of host names, addresses, accounts and paths, kept outside
every git checkout, see reference/values-file.md — with its sha256 recorded;
printable strings of binaries and the members of zip and tar archives). `bundle`
writes the text files as prompt-sized bundles (the Codex and Grok stages read
these). `seal` accepts RUN/verdict.json only for the staged digest, with every
stage's artefact present or the stage waived in the owner's words, and only when
the scan matched the values file and that file has not changed since. `verify`
stages the sources again into a temporary directory and compares digests.

`approve` records the owner's words in RUN/approvals/STEP.json, bound to the step,
the repository's id on GitHub, the digest, the seal, the pushes, an expiry and (for
publish) the preflight's match hash and the release; each record covers one action (a
publish record: one switch and the one release it names), and `revoke` closes it.
`push` needs a valid push approval, verifies again and pushes, for each --git source,
the exact commit that was checked (never HEAD, never --force). `preflight` needs
approval 1 used for the repository, then reads every ref, issue, pull request,
release, tag, label, Pages site, wiki repository, Actions record and setting of a new
private repository and writes RUN/preflight.json. `publish` needs a valid publish
approval and the same preflight result, switches the repository to public, reads the
inventory again and switches back to private if anything arrived in between, creates
the approved release from the run's staged copies (--target the checked commit, never
--generate-notes), and reads both back without credentials; RUN/state.json records each
step before and after it, so a run that stopped half-way resumes from what GitHub
actually shows. One push or publish runs at a time per run (RUN/approvals/.lock).

A secret value never reaches this script's output in full: secret-kind hits print
as a kind and a short redacted preview, and a private value prints as its key.

Exit: 0 ok, clean, sealed, same, approved, pushed, ready or published; 1 scan hits,
gate closed, seal refused, digest differs, an approval missing or refused, a preflight
refused, or a push, switch or release failed; 2 usage, or a tree changed after
staging; 3 git, gitleaks or gh missing.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:  # Python 3.14+ reads zstd (a .tar.zst); an older one names such a stream as unread
    from compression import zstd as _zstd  # type: ignore[import-not-found,unused-ignore]
except ImportError:
    _zstd = None

#: A text file larger than this is scanned mechanically only (no AI reads it):
#: built artefacts such as a 19 MB release page. The final check names them.
AI_MAX_BYTES = 800_000
#: One bundle: about 250k tokens of code, half of grok-4.7's 500k window.
BUNDLE_MAX_BYTES = 900_000
#: Archive members read for the scan (larger ones are named for the session).
MEMBER_MAX_BYTES = 5_000_000
#: One archive's members, read one at a time: past this many bytes in all, or this many
#: members, the scan stops with no report (an archive this large is split before upload).
ARCHIVE_MAX_BYTES = 200_000_000
ARCHIVE_MAX_MEMBERS = 50_000
STAGES = ("mechanical", "claude", "codex", "grok", "final")
#: The model family the Claude stages (2 and 5) must report in `ran_on` (SKILL.md's table
#: pins Opus). Change it here, in one place, if the skill's table pins another model.
CLAUDE_STAGE_MODEL = "opus"
GITLEAKS = os.environ.get("AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN") or shutil.which("gitleaks") or ""
#: The GitHub CLI, logged in with access to the destination (approve, preflight, publish).
GH = os.environ.get("AGRODESIGN_GITHUB_PUBLISH_GH_BIN") or shutil.which("gh") or ""
#: The REST API the anonymous check reads after a switch (no credentials are sent).
API_URL = os.environ.get("AGRODESIGN_GITHUB_PUBLISH_API_URL", "https://api.github.com").rstrip("/")
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
TAG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,99}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
#: Seconds between the reads right after a switch (GitHub can lag a moment); tests shorten it.
RETRY_SECONDS = float(os.environ.get("AGRODESIGN_GITHUB_PUBLISH_RETRY_SECONDS", "3"))
#: Labels every new GitHub repository starts with; any other label is text nobody checked.
DEFAULT_LABELS = frozenset({"bug", "documentation", "duplicate", "enhancement", "good first issue", "help wanted", "invalid", "question", "wontfix"})
PAGE_SIZE = 100
#: GitHub keeps a repository's activity log (ref changes) for this many days.
ACTIVITY_DAYS = 90
MAX_PAGES = 100
#: How long an approval stays usable unless the owner gives another window (minutes).
APPROVAL_STEPS = ("push", "publish", "release")
VALID_MINUTES = {"push": 24 * 60, "publish": 60, "release": 60}
ENCODINGS = ("utf-8", "cp932", "euc_jp")

#: Secret-shaped text: always a hit, printed redacted.
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "api_key": re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{32,}|xai-[A-Za-z0-9]{32,}|AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,})"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    "url_with_password": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]{3,}@[^\s'\"]+", re.IGNORECASE),
}
#: Internal-information-shaped text: a hit for the session to triage, printed in full.
INFO_PATTERNS = {
    "private_ip": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2}"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})\b|\bf[cd][0-9a-f]{2}:[0-9a-f]{0,4}:[0-9a-f:]{2,}",
        re.IGNORECASE,
    ),
    "internal_hostname": re.compile(
        r"(?:(?<=://)|(?<=@)|(?<=\s)|^)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:local|lan|internal|intranet|corp|home\.arpa)(?=[:/\s'\"]|$)",
        re.IGNORECASE,
    ),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "host_path": re.compile(r"(?<![\w.])(?:/home/[a-z_][\w-]*|/Users/[A-Za-z_][\w.-]*|/root/|/srv/[\w.-]+|/mnt/[\w.-]+|[A-Za-z]:\\Users\\[^\\\s]+)"),
    "env_variable": re.compile(r"\b(?:DATABASE_URL|PGPASSWORD|POSTGRES_PASSWORD|SECRET_KEY|JWT_SECRET)\b"),
}
#: Addresses that are public by design and never a finding.
PUBLIC_EMAILS = re.compile(r"(?:^noreply@anthropic\.com$|@users\.noreply\.github\.com$|^noreply@github\.com$|^git@github\.com$|@example\.(?:com|org|net)$)", re.IGNORECASE)
SENSITIVE_NAMES = re.compile(
    r"(?:^\.env(?:\..*)?$|\.pem$|\.key$|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$|\.p12$|\.pfx$|\.kdbx$|^\.npmrc$|^\.pypirc$|^\.netrc$|^\.git-credentials$|^credentials\.json$|^auth\.json$|^\.htpasswd$)",
    re.IGNORECASE,
)
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{8,}")
#: Distinct hits one scan may hold; past it the scan stops and writes no report (fail closed).
MAX_HITS_TOTAL = 20_000
#: An archive or compressed stream, told by its signature (the group's name is its kind)
#: before a payload is read as text. A tar has none at its start (its ustar field, or a
#: parse, tells it); an LZMA-alone stream is told by its header (_lzma_alone).
COMPRESSED_HEAD = re.compile(
    rb"\A(?:(?P<zip>PK\x03\x04|PK\x05\x06)|(?P<gzip>\x1f\x8b)|(?P<bzip2>BZh[1-9](?:1AY&SY|\x17rE8P\x90))|(?P<xz>\xfd7zXZ\x00)"
    rb"|(?P<zstd>\x28\xb5\x2f\xfd|[\x50-\x5f]\x2a\x4d\x18)|(?P<sevenzip>7z\xbc\xaf\x27\x1c)|(?P<rar>Rar!\x1a\x07)|(?P<lz4>\x04\x22\x4d\x18)|(?P<lzip>LZIP))"
)
#: Compressed streams the scan decompresses (zstd only where Python has compression.zstd).
STREAM_KINDS = ("gzip", "bzip2", "xz", "lzma", "zstd")
#: A zstd skippable frame (magic 0x184D2A50-5F): its payload is plain bytes the decompressor
#: drops, so the scan reads it apart.
ZSTD_SKIPPABLE = re.compile(rb"\A[\x50-\x5f]\x2a\x4d\x18")
#: What a decompressor is fed per call: the first call of each member gets the smaller,
#: each later call twice the last, up to the larger. What a member leaves unused at its end
#: (copied by the module as unused_data) is then at most twice what it consumed, or the
#: first step: many small members or frames cost linear time, not the rest of the input each.
STREAM_STEP_BYTES = (64, 1 << 20)
#: Formats the scan cannot open: named as unread, never read as printable strings.
UNREAD_KINDS = {"sevenzip": "7z", "rar": "RAR", "lz4": "LZ4", "lzip": "lzip"}
#: How much of a member name or a shown value a hit keeps (report.json stays near the
#: input's size); the full text stays in the dedupe key.
LABEL_MAX_CHARS = 240
#: Further line numbers a counted hit lists after its first.
MORE_LINES_MAX = 20
LFS_POINTER = b"version https://git-lfs.github.com/spec/"
#: Long base64 runs (an inlined payload such as a built page's compressed app) are
#: decoded, gunzipped when gzip, and scanned as text one level deep.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")
DECODED_MAX_BYTES = 60_000_000
MAX_HITS_SHOWN = 60


def _tar_read(tf: tarfile.TarFile, ti: tarfile.TarInfo) -> bytes:
    """A tar member's bytes (b"" when tarfile gives no file object for it)."""
    fh = tf.extractfile(ti)
    return fh.read() if fh is not None else b""


def _tar_reader(tf: tarfile.TarFile, ti: tarfile.TarInfo) -> Callable[[], bytes] | None:
    """How a tar member's content is read for the scan: a file's bytes (None past
    MEMBER_MAX_BYTES), a link's target as text, nothing for a directory (its name is
    scanned anyway); None for anything else (a device or FIFO), named as unread."""
    if ti.isfile():
        return (lambda: _tar_read(tf, ti)) if ti.size <= MEMBER_MAX_BYTES else None
    if ti.issym() or ti.islnk():
        return lambda: ti.linkname.encode("utf-8", "surrogateescape")
    if ti.isdir():
        return lambda: b""
    return None


def opens_as_tar(data: bytes) -> bool:
    """Whether ``data`` opens as an uncompressed tar with at least one member, in any header
    format (a v7 tar has no ustar marker): compressed_kind does not tell a tar. Only the
    first header is parsed, so a 5 MB member costs one block."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            return tf.next() is not None
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return False


def _lzma_alone(data: bytes) -> bool:
    """An LZMA-alone (.lzma) header, checked as liblzma's own format detection checks it:
    a properties byte below 225, a dictionary size of 2^n or 2^n + 2^(n-1) (or unset) of
    at least 4 KiB, and an uncompressed size unknown or below 2^38. The format has no
    signature, and these rule out nearly every other binary."""
    if len(data) < 13 or data[0] >= 225:
        return False
    dict_size = int.from_bytes(data[1:5], "little")
    size = int.from_bytes(data[5:13], "little")
    if size != 2**64 - 1 and size >= 2**38:
        return False
    if dict_size == 2**32 - 1:
        return True
    if dict_size < 4096:
        return False
    n = dict_size.bit_length() - 1
    return dict_size in (1 << n, (1 << n) + (1 << (n - 1)))


def compressed_kind(data: bytes) -> str | None:
    """The archive or compressed-stream kind ``data`` starts with (a COMPRESSED_HEAD group
    name, or "lzma"), or None. A tar is not told here."""
    m = COMPRESSED_HEAD.match(data)
    if m is not None:
        return m.lastgroup
    return "lzma" if _lzma_alone(data) else None


def _decompressor(kind: str) -> Any:
    """A fresh decompressor for one member or stream of ``kind`` (one of STREAM_KINDS)."""
    import bz2

    if kind == "gzip":
        return zlib.decompressobj(zlib.MAX_WBITS | 16)
    if kind == "bzip2":
        return bz2.BZ2Decompressor()
    if kind == "xz":
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    if kind == "lzma":
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    return _zstd.ZstdDecompressor()


def decompress_single(data: bytes) -> tuple[bytes | None, bool, bytes]:
    """(a lone gzip, bzip2, xz, LZMA-alone or zstd file's content, every concatenated
    member, stream or frame, capped; whether it was decompressed to its end; the payloads
    of zstd skippable frames, which are no part of the content), or (None, True, b"")
    when it is none of them (zstd on a Python without compression.zstd included:
    Scanner.binary names it). A cap reached, a cut stream or trailing data is reported
    as incomplete (with None when nothing came out, so the raw bytes are read instead).
    The input is walked by offset in growing steps and the pieces joined once, so the
    time is linear in the input however many members or frames it holds (copying
    the rest of the input per frame would be quadratic)."""
    kind = compressed_kind(data)
    if kind not in STREAM_KINDS or (kind == "zstd" and _zstd is None):
        return None, True, b""
    broken: tuple[type[BaseException], ...] = (zlib.error, OSError, EOFError, ValueError, lzma.LZMAError)
    if _zstd is not None:
        broken += (_zstd.ZstdError,)
    view = memoryview(data)
    out: list[bytes] = []
    side: list[bytes] = []
    size = pos = 0

    def result(complete: bool) -> tuple[bytes | None, bool, bytes]:
        return (b"".join(out) if size or complete else None), complete, b"".join(side)

    try:
        while pos < len(data):
            if kind == "zstd" and ZSTD_SKIPPABLE.match(data[pos : pos + 4]):  # magic, a 4-byte size, then plain bytes
                end = pos + 8 + int.from_bytes(data[pos + 4 : pos + 8], "little")
                side.append(data[pos + 8 : end])
                if end > len(data):
                    return result(False)
                pos = end
                continue
            d = _decompressor(str(kind))
            step = STREAM_STEP_BYTES[0]
            while not d.eof:
                chunk = view[pos : pos + step]
                if not chunk:  # the input ended inside a member
                    return result(False)
                got = d.decompress(chunk, DECODED_MAX_BYTES - size)
                out.append(got)
                size += len(got)
                if size >= DECODED_MAX_BYTES:
                    return result(False)
                # Below the cap every decompressor took the whole chunk (zlib keeps an
                # unconsumed tail only at its output limit), but what follows a member's end.
                pos += len(chunk) - (len(d.unused_data) if d.eof else 0)
                step = min(step * 2, STREAM_STEP_BYTES[1])
            if pos < len(data) and compressed_kind(data[pos : pos + 16]) != kind:
                return result(False)
    except broken:
        return result(False)
    return result(True)


def zip_directory_entries(data: bytes) -> int:
    """How many central-directory entries zipfile would build for ``data``, counted before
    it parses them: the entry signatures in the directory that ends at the zip's end
    record (the record's directory size back from it, and the 76 bytes of zip64 records
    some Pythons place between), never in the whole bytes, where a tar of jars in front of
    the zip holds signatures of its own. zipfile builds an entry only where the signature
    starts one, so this bounds its memory. Every signature, without zipfile's end-record
    reader (a Python that renamed it)."""
    try:
        endrec = zipfile._EndRecData(io.BytesIO(data))  # type: ignore[attr-defined]
        end, size = int(endrec[zipfile._ECD_LOCATION]), int(endrec[zipfile._ECD_SIZE])  # type: ignore[attr-defined]
    except (AttributeError, TypeError, IndexError, OSError, ValueError):
        return data.count(b"PK\x01\x02")
    return data[max(0, end - size - 76) : end].count(b"PK\x01\x02")


def decode_blob(run: str) -> tuple[str | None, bytes | None, bool]:
    """(a base64 run as text, gunzipped when gzip; or, for a binary payload (wasm, a zip
    or wheel, an image, a data file), its bytes for Scanner.binary — both None when it
    is not base64; whether the decoding was complete). A gzip stream cut at the size
    cap, unfinished, or followed by more data is incomplete: the scan reports it as not
    fully read."""
    try:
        raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
    except ValueError:
        return None, None, True
    complete = True
    if raw[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(zlib.MAX_WBITS | 16)
        try:
            raw = d.decompress(raw, DECODED_MAX_BYTES)
        except zlib.error:
            return None, None, False
        complete = d.eof and not d.unconsumed_tail and not d.unused_data
    if compressed_kind(raw) or raw[257:262] == b"ustar":  # an archive, even one that reads as UTF-8
        return None, raw, complete
    try:
        return raw.decode("utf-8"), None, complete
    except UnicodeDecodeError:  # a gzip'd wasm, zip or wheel block inlined in a page
        return None, raw, complete


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_encoding(path: Path) -> str | None:
    """The encoding the WHOLE file decodes in strictly (UTF-16 with a BOM, UTF-8,
    Shift_JIS, EUC-JP), or None: binary, scanned by its printable strings. Deciding on
    the whole file keeps a Shift_JIS tail behind an ASCII head from being read as
    replacement characters."""
    data = path.read_bytes()
    candidates = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else (() if b"\0" in data else ENCODINGS)
    for enc in candidates:
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return None


def strict_decode(data: bytes) -> str | None:
    """``data`` decoded strictly (UTF-16 with a BOM, UTF-8, Shift_JIS, EUC-JP), or None."""
    for enc in ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def safe(text: str) -> str:
    """``text`` with every secret-shaped match cut to a redacted preview: for file
    names, link targets and destination URLs, which the report and stdout show."""
    for rx in SECRET_PATTERNS.values():
        text = rx.sub(lambda m: _redact(m.group(0)), text)
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", text)


def read_text(path: Path, enc: str) -> str:
    return path.read_bytes().decode(enc, errors="replace")


#: git as a push sees the objects: replace refs off (a push sends the real objects,
#: never their local replacements).
GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def git(repo: Path, *args: str, binary: bool = False, stdin: bytes | None = None) -> bytes | str:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, input=stdin, env=GIT_ENV)
    except FileNotFoundError:
        die("git missing", 3)
    except subprocess.CalledProcessError as exc:
        die(f"git {' '.join(args[:2])} failed in {repo}: {exc.stderr.decode(errors='replace').strip()[:300]}")
    return out.stdout if binary else out.stdout.decode("utf-8", errors="replace")


# ── sources ────────────────────────────────────────────────────────────────────


@dataclass
class Source:
    kind: str  # tree | git | file | meta
    path: Path
    remote: str = ""
    ref: str = ""
    whole: bool = False  # --whole-history: every commit from the root, whatever the destination holds
    repo: str = ""  # --repo-meta OWNER/NAME


SOURCE_USAGE = "--tree DIR, --git REPO --push REMOTE REF [--whole-history], --file PATH or --repo-meta OWNER/NAME"


def parse_sources(argv: list[str]) -> list[Source]:
    """The SOURCE arguments in the order given (argparse would group them by kind)."""
    out: list[Source] = []
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in ("--tree", "--file") and i + 1 < len(argv):
            out.append(Source(flag[2:], Path(argv[i + 1]).resolve()))
            i += 2
        elif flag == "--git" and i + 4 < len(argv) and argv[i + 2] == "--push":
            remote, ref = argv[i + 3], argv[i + 4]
            if remote.startswith("-") or not re.fullmatch(r"[\w./@^~:-]+", ref) or ref.startswith("-"):
                die(f"--push needs REMOTE REF, got {remote!r} {ref!r}")
            whole = i + 5 < len(argv) and argv[i + 5] == "--whole-history"
            out.append(Source("git", Path(argv[i + 1]).resolve(), remote, ref, whole=whole))
            i += 6 if whole else 5
        elif flag == "--repo-meta" and i + 1 < len(argv):
            name = argv[i + 1]
            if not REPO_NAME.fullmatch(name):
                die(f"--repo-meta needs OWNER/NAME, got {name!r}")
            out.append(Source("meta", Path("."), repo=name))
            i += 2
        else:
            die(f"unknown or incomplete source argument at {flag!r} (use {SOURCE_USAGE})")
    if not out:
        die(f"give at least one source: {SOURCE_USAGE}")
    if sum(1 for s in out if s.kind == "git") > 1:  # two pushes, one failing, would publish half
        die("one --git destination per run: stage, check and push each repository on its own")
    if sum(1 for s in out if s.kind == "meta") > 1:
        die("one --repo-meta per run: the one repository this run publishes")
    return out


def push_url(repo: Path, remote: str) -> str:
    """The one URL a push to ``remote`` goes to (pushurl and pushInsteadOf applied). A
    remote with several push URLs, or a push URL other than its fetch URL, is refused:
    the set must be computed against, and pushed to, the same single place."""
    names = str(git(repo, "remote")).split()
    if remote in names:
        pushes = str(git(repo, "remote", "get-url", "--push", "--all", remote)).split()
        fetches = str(git(repo, "remote", "get-url", "--all", remote)).split()
        if len(pushes) != 1 or pushes != fetches:
            die(f"remote {remote}: its push URL differs from its fetch URL, or it has several — give the one destination URL instead of the remote name")
        url = pushes[0]
    else:
        url = remote  # a URL or a path, used as given
    if re.match(r"^https?://[^/]*@", url, re.IGNORECASE) or re.match(r"^[a-z][a-z0-9+.-]*://[^/@]*:[^/@]*@", url, re.IGNORECASE):
        die("the destination URL holds credentials (a user part in an http(s) URL, or a password) — use a credential helper and give the URL without them")
    # url.<base>.insteadOf / pushInsteadOf would send the push somewhere else than ls-remote asked.
    rules = str(subprocess.run(["git", "-C", str(repo), "config", "--get-regexp", r"^url\..*\.(push)?insteadof$"],
                               capture_output=True, text=True, env=GIT_ENV).stdout).splitlines()  # fmt: skip
    for rule in rules:
        _, _, prefix = rule.partition(" ")
        if prefix and url.startswith(prefix):
            die(f"git config rewrites URLs starting {prefix!r} (insteadOf) — the checked and the pushed destination could differ; remove the rule for this push")
    return url


def remote_shas(repo: Path, url: str) -> list[str]:
    """What `git push` negotiates with: the destination's branch and tag tips that this
    clone also has. A tip the clone lacks cannot shrink what a push sends, and other
    refs (GitHub's refs/pull/*) are not offered to a push, so neither may shrink the set."""
    refs = str(git(repo, "ls-remote", "--heads", "--tags", url)).splitlines()
    shas = sorted({ln.split("\t", 1)[0] for ln in refs if "\t" in ln})
    return [s for s in shas if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", s], capture_output=True, env=GIT_ENV).returncode == 0]


def refuse_odd_history(repo: Path, commit: str) -> None:
    """Local state that makes history look other than what a push sends, and LFS."""
    if str(git(repo, "rev-parse", "--is-shallow-repository")).strip() == "true":
        die(f"{repo}: a shallow clone — the set cannot be known; clone it in full")
    grafts = Path(str(git(repo, "rev-parse", "--git-path", "info/grafts")).strip())
    if not grafts.is_absolute():
        grafts = repo / grafts
    if grafts.exists():
        die(f"{repo}: {grafts} rewrites history locally — remove it first")
    info_attrs = Path(str(git(repo, "rev-parse", "--git-path", "info/attributes")).strip())
    if not info_attrs.is_absolute():
        info_attrs = repo / info_attrs
    if info_attrs.exists() and re.search(r"filter\s*=\s*lfs", info_attrs.read_text(errors="replace")):
        die(f"{repo}: {info_attrs} uses Git LFS — the LFS objects are not in the set")
    listing = str(git(repo, "ls-tree", "-r", "-z", "--name-only", commit)).split("\0")
    for name in listing:
        if name.endswith(".gitattributes"):
            body = str(git(repo, "cat-file", "blob", f"{commit}:{name}"))
            if re.search(r"filter\s*=\s*lfs", body):
                die(f"{repo}: {name} uses Git LFS — the LFS objects are not in the set; stage them as files or do not use LFS")


def full_ref(repo: Path, ref: str) -> str:
    name = str(git(repo, "rev-parse", "--symbolic-full-name", ref)).strip()
    if not name.startswith("refs/"):
        die(f"--push ... {ref}: not a branch or tag name (a push needs one)")
    return name


class Stager:
    """Copies the upload set into ``tree`` and builds the manifest."""

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.entries: list[dict[str, object]] = []
        self.identities: set[str] = set()
        self.pushes: list[dict[str, str]] = []

    def add(self, dest: Path, origin: str, *, link: str | None = None, executable: bool = False) -> None:
        enc = text_encoding(dest)
        size = dest.stat().st_size
        entry: dict[str, object] = {
            "path": dest.relative_to(self.tree).as_posix(), "origin": origin, "size": size,
            "sha256": sha256_file(dest), "text": enc is not None, "encoding": enc,
            "ai": enc is not None and size <= AI_MAX_BYTES, "exec": executable,
        }  # fmt: skip
        if link is not None:
            entry["symlink_target"] = link
        self.entries.append(entry)

    def write_new(self, dest: Path, data: bytes, sha: str) -> Path:
        """Write ``data`` at ``dest``, or at ``dest~<full sha>`` when another version took
        the name; never over an existing file (exclusive create)."""
        # A path that is a file in one commit and a directory in another cannot hold both:
        # the later one goes under (collision)/, its real path kept in the manifest's origin.
        collision = self.tree / dest.relative_to(self.tree).parts[0] / "(collision)" / f"{sha}~{dest.name}"
        for cand in (dest, dest.with_name(f"{dest.name}~{sha}"), collision):
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                with cand.open("xb") as fh:
                    fh.write(data)
                return cand
            except (FileExistsError, NotADirectoryError, IsADirectoryError):
                continue
        die(f"two staged objects claim {dest} and {dest.name}~{sha}")
        raise AssertionError

    def stage_tree(self, n: int, src: Path) -> None:
        if not src.is_dir():
            die(f"--tree {src}: not a directory")
        base = self.tree / f"{n:02d}-tree-{src.name}"

        def fail(err: OSError) -> None:
            die(f"--tree {src}: cannot read {err.filename}: {err.strerror}")

        for root, dirs, files in os.walk(src, followlinks=False, onerror=fail):
            keep = []
            for d in sorted(dirs):
                p = Path(root) / d
                if d == ".git":
                    continue
                if p.is_symlink():
                    files.append(d)  # recorded as a link below, never followed
                else:
                    keep.append(d)
            dirs[:] = keep
            for name in sorted(files):
                p = Path(root) / name
                rel = p.relative_to(src)
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if p.is_symlink():
                    target = os.readlink(p)
                    dest.write_text(target, encoding="utf-8")
                    self.add(dest, f"tree:{rel.as_posix()}", link=target)
                elif p.is_file():
                    shutil.copyfile(p, dest)
                    self.add(dest, f"tree:{rel.as_posix()}", executable=bool(p.stat().st_mode & 0o111))
                else:
                    die(f"--tree {src}: {rel} is neither a file nor a link")

    def stage_file(self, n: int, src: Path) -> None:
        if not src.is_file():
            die(f"--file {src}: not a file")
        dest = self.tree / f"{n:02d}-file" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        self.add(dest, f"file:{src.name}")

    def stage_meta(self, n: int, name: str) -> None:
        """The repository's description, homepage and topics: public once the repository
        is, so they are staged and read like every file (a re-stage fetches them again)."""
        try:
            body = public_meta(gh_repo(name))
        except GhError as exc:
            die(f"--repo-meta {name}: {exc}")
        dest = self.tree / f"{n:02d}-repo" / "repository.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        self.add(dest, f"repo:meta:{name}")

    def stage_git(self, n: int, src: Source, recorded: list[dict[str, object]] | None = None) -> None:
        repo = src.path
        ref = full_ref(repo, src.ref)
        url = push_url(repo, src.remote)
        tip = str(git(repo, "rev-parse", "--verify", f"{src.ref}^{{object}}")).strip()
        commit = str(git(repo, "rev-parse", "--verify", f"{src.ref}^{{commit}}")).strip()
        refuse_odd_history(repo, commit)
        # What the destination already held when the set was first staged ("base") is
        # recorded, and a re-stage (verify, push) uses that record, not the live remote:
        # otherwise a verify after the push would stage nothing and never match. --whole-history
        # ignores the destination altogether, so the run reads every commit from the root.
        row = next((r for r in recorded or [] if r.get("url") == url and r.get("ref") == ref), None)
        if src.whole:
            have: list[str] = []
        elif row is not None:
            have = [str(s) for s in row.get("base", [])]  # type: ignore[union-attr]
        else:
            have = remote_shas(repo, url)
        base = self.tree / f"{n:02d}-git"
        self.pushes.append({"repo": str(repo), "url": url, "ref": ref, "tip": tip, "commit": commit, "base": have, "whole_history": src.whole})  # type: ignore[dict-item]
        # The ref's name and the destination become public too: a staged text file, so the
        # scan and every reviewer read them (the URL with any user part masked).
        meta = base / "PUSH.txt"
        meta.parent.mkdir(parents=True, exist_ok=True)
        history = "history whole (every commit from the root)\n" if src.whole else "".join(f"after {s}\n" for s in have)
        meta.write_text(f"destination {safe(url)}\nref {ref}\ntip {tip}\n{history}", encoding="utf-8")
        self.add(meta, f"git:push:{ref}")
        if tip in have:
            return  # the destination already has it: nothing is sent
        obj = tip  # every annotated tag on the way to the commit is sent, so each is staged
        while str(git(repo, "cat-file", "-t", obj)).strip() == "tag":
            raw = git(repo, "cat-file", "tag", obj, binary=True)
            assert isinstance(raw, bytes)
            self._identities(raw)
            self.add(self.write_new(base / "tags" / f"{obj}.txt", raw, obj), f"git:tag:{obj}")
            obj = raw.split(b"\n", 1)[0].split(b" ", 1)[1].decode().strip()
            if obj in have:
                break
        revs = ("\n".join([commit, *[f"^{s}" for s in have]]) + "\n").encode()
        listing = str(git(repo, "rev-list", "--objects", "--stdin", stdin=revs))
        paths: dict[str, str] = {}
        for line in listing.splitlines():
            sha, _, path = line.partition(" ")
            paths.setdefault(sha, path)
        if not paths:
            return
        kinds = str(git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=("\n".join(paths) + "\n").encode()))
        for line in kinds.splitlines():
            sha, _, kind = line.partition(" ")
            if kind == "commit":
                raw = git(repo, "cat-file", "commit", sha, binary=True)
                assert isinstance(raw, bytes)
                self._identities(raw)
                self.add(self.write_new(base / "commits" / f"{sha}.txt", raw, sha), f"git:commit:{sha}")
            elif kind == "tree":
                # Names, modes, links and submodule pointers of every new tree: a rename
                # sends a new name for an old blob, and only the tree carries it.
                raw_tree = git(repo, "ls-tree", "-z", sha, binary=True)
                assert isinstance(raw_tree, bytes)
                lines = []
                for ent in raw_tree.split(b"\0"):
                    if ent:
                        meta, _, raw_name = ent.partition(b"\t")
                        name = strict_decode(raw_name)  # the name as pushed, never replacement characters
                        if name is None:
                            die(f"{repo}: tree {sha} holds a file name in no known encoding — rename it first ({raw_name[:40]!r})")
                        lines.append(f"{meta.decode()}\t{json.dumps(name, ensure_ascii=False)}")
                label = paths[sha] or "(root)"
                body = (f"# tree {sha} at {json.dumps(label, ensure_ascii=False)}\n" + "\n".join(lines) + "\n").encode()
                self.add(self.write_new(base / "trees" / f"{sha}.txt", body, sha), f"git:tree:{label}")
            elif kind == "blob":
                rel = paths[sha] or f"(no path)/{sha}"
                data = git(repo, "cat-file", "blob", sha, binary=True)
                assert isinstance(data, bytes)
                if data.startswith(LFS_POINTER):
                    die(f"{repo}: {rel} is a Git LFS pointer — its object is not in the set; stage it as a file or do not use LFS")
                self.add(self.write_new(base / "files" / rel, data, sha), f"git:blob:{rel}")

    def _identities(self, raw: bytes) -> None:
        for line in raw.decode("utf-8", errors="replace").split("\n\n", 1)[0].splitlines():
            key, _, rest = line.partition(" ")
            if key in ("author", "committer", "tagger"):
                self.identities.add(re.sub(r" \d+ [+-]\d{4}$", "", rest))

    def manifest(self) -> dict[str, object]:
        self.entries.sort(key=lambda e: str(e["path"]))
        return {"entries": self.entries, "identities": sorted(self.identities), "pushes": self.pushes}


def stage_into(sources: list[Source], tree: Path, recorded: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Stage every source; ``recorded`` (the sealed manifest's push rows) fixes each push's base."""
    st = Stager(tree)
    for n, src in enumerate(sources, 1):
        if src.kind == "tree":
            st.stage_tree(n, src.path)
        elif src.kind == "file":
            st.stage_file(n, src.path)
        elif src.kind == "meta":
            st.stage_meta(n, src.repo)
        else:
            st.stage_git(n, src, recorded)
    return st.manifest()


def digest_of(manifest: dict[str, object]) -> str:
    """What the seal binds: every staged path, its bytes, link target and exec bit; the identities; each push's tip."""
    core = {
        "entries": [[e["path"], e["sha256"], e.get("symlink_target"), bool(e.get("exec"))] for e in manifest["entries"]],  # type: ignore[union-attr]
        "identities": manifest["identities"],
        "pushes": manifest.get("pushes", []),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_manifest(run: Path) -> dict[str, object]:
    try:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        die(f"{run}/manifest.json missing or unreadable: run stage first")
    if not isinstance(manifest, dict) or "entries" not in manifest:
        die(f"{run}/manifest.json is not a stage manifest")
    return manifest


def checked_manifest(run: Path) -> dict[str, object]:
    """The manifest, after proving RUN/tree still holds exactly the staged bytes."""
    manifest = load_manifest(run)
    tree = run / "tree"
    want = {str(e["path"]): str(e["sha256"]) for e in manifest["entries"]}  # type: ignore[union-attr]
    have = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file() or p.is_symlink()}
    problems = [f"extra file {p}" for p in sorted(have - set(want))] + [f"missing file {p}" for p in sorted(set(want) - have)]
    for path, sha in want.items():
        if path in have and sha256_file(tree / path) != sha:
            problems.append(f"changed file {path}")
    digest = digest_of(manifest)
    recorded = (run / "DIGEST").read_text().strip() if (run / "DIGEST").exists() else ""
    if digest != manifest.get("digest") or digest != recorded:
        problems.append("the manifest does not match DIGEST")
    if problems:
        die("TREE=changed after staging — stage again: " + "; ".join(problems[:5]))
    return manifest


def stage_problems(run: Path, manifest: dict[str, object], stages: dict[str, object], *, upto: str = "final") -> list[str]:
    """What is missing among the artefacts of each stage not waived, up to ``upto``: "claude" (stages 1-2, the gate before Codex),
    "codex" (1-3, the gate before Grok), "grok" (1-4) or "final" (all)."""
    digest = manifest.get("digest")
    problems: list[str] = []

    def waived(name: str) -> bool:
        st = stages.get(name)
        return isinstance(st, dict) and st.get("status") == "waived" and bool(str(st.get("owner_words") or "").strip())

    report = run / "scan" / "report.json"
    try:
        scan = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        scan = None
    if not isinstance(scan, dict) or scan.get("digest") != digest:
        problems.append("mechanical: no scan report for this digest")
    elif scan.get("hits") and not (run / "triage.md").is_file():
        problems.append("mechanical: the scan had hits and there is no triage.md")
    entries = manifest.get("entries")
    ai_files = [str(e.get("path")) for e in entries if isinstance(e, dict) and e.get("ai")] if isinstance(entries, list) else []
    if not waived("claude"):
        answers = sorted(run.glob("stage2-claude*.json"))
        if not answers:
            problems.append("claude: no stage2-claude*.json")
        for a in answers:
            got = _json_file(a)
            if not isinstance(got, dict) or not isinstance(got.get("findings"), list) or got.get("digest") != digest:
                problems.append(f"claude: {a.name} is not {{digest, findings: [...]}} for this digest")
            elif CLAUDE_STAGE_MODEL not in str(got.get("ran_on", "")).lower():
                problems.append(f"claude: {a.name} has no ran_on naming {CLAUDE_STAGE_MODEL.capitalize()} (the model the agent's system prompt named)")
            else:
                problems += [f"claude: {p}" for p in answer_shape_problems(a.name, got, ai_files)]
    index = _json_file(run / "bundles" / "index.json")
    listed = index.get("bundles") if isinstance(index, dict) and index.get("digest") == digest else None
    on_disk = {b.name for b in (run / "bundles").glob("bundle-*.md")}
    if not isinstance(listed, dict) or not listed or set(listed) != on_disk:
        problems.append("bundles: bundles/index.json missing, for another digest, or not the bundles on disk")
        listed = {}
    if upto == "claude":
        return problems
    if not waived("codex"):
        for name in sorted(listed):
            problems += codex_problems(run, name)
    if upto == "codex":
        return problems
    if not waived("grok"):
        for name, sha in sorted(listed.items()):
            nn = name.removeprefix("bundle-").removesuffix(".md")
            g = _json_file(run / f"stage4-grok-{nn}.json")
            if sha256_file(run / "bundles" / name) != sha:
                problems.append(f"grok: {name} changed after bundling")
            elif not isinstance(g, dict) or g.get("bundle_sha256") != sha or g.get("digest") != digest:
                problems.append(f"grok: no answer for {name} at this digest")
    if upto == "final" and not waived("final"):
        final = _json_file(run / "stage5-final.json")
        if not isinstance(final, dict) or final.get("verdict") != "PUBLISH_OK" or final.get("digest") != digest or CLAUDE_STAGE_MODEL not in str(final.get("ran_on", "")).lower():
            problems.append(f"final: stage5-final.json is not PUBLISH_OK for this digest with a ran_on naming {CLAUDE_STAGE_MODEL.capitalize()}")
        elif not isinstance(final.get("confirmed"), list):
            problems.append("final: stage5-final.json has no confirmed list")
        elif any(not isinstance(c, dict) or str(c.get("severity", "")).strip().lower() not in ("medium", "low") for c in final["confirmed"]):
            problems.append("final: PUBLISH_OK with a confirmed finding that is critical, high or of no known severity — fix it and stage again")
    return problems


def codex_problems(run: Path, bundle_name: str) -> list[str]:
    """Codex read this bundle: its brief `stage3-codex-NN.md` holds the bundle's whole
    text, and its answer `.codex.md` ends in a findings JSON block."""
    nn = bundle_name.removeprefix("bundle-").removesuffix(".md")
    brief, answer = run / f"stage3-codex-{nn}.md", run / f"stage3-codex-{nn}.md.codex.md"
    text = (run / "bundles" / bundle_name).read_text(encoding="utf-8", errors="replace")
    if not brief.is_file() or text not in brief.read_text(encoding="utf-8", errors="replace"):
        return [f"codex: {brief.name} missing or without {bundle_name}'s whole text"]
    got = _last_json_block(answer.read_text(encoding="utf-8", errors="replace")) if answer.is_file() else None
    if not isinstance(got, dict) or not isinstance(got.get("findings"), list):
        return [f"codex: no answer with a findings JSON block ({answer.name})"]
    index = _json_file(run / "bundles" / "index.json")
    lists = index.get("files") if isinstance(index, dict) else None
    files = lists.get(bundle_name) if isinstance(lists, dict) else None
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return [f"codex: bundles/index.json lists no files for {bundle_name} (bundle again)"]
    # The lists are the script's own, but checked, not trusted: together, in
    # bundle-number order, they are the manifest's AI-readable files in order; and the
    # bundle is exactly what `bundle` writes for its files (so a FILE-header-shaped
    # line inside a file's text cannot stand in for a file the bundle lacks).
    manifest = _json_file(run / "manifest.json")
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    ai = [e for e in entries if isinstance(e, dict) and e.get("ai")] if isinstance(entries, list) else None
    names = sorted(lists, key=_bundle_number)
    bundles = index.get("bundles") if isinstance(index, dict) else None
    # One list per bundle the index hashes (stage_problems holds those to the files on
    # disk), numbered 1..n: a list for a bundle that does not exist would take its files
    # out of every AI stage while the lists still add up to the manifest.
    if not isinstance(bundles, dict) or set(bundles) != set(lists) or [_bundle_number(n) for n in names] != list(range(1, len(names) + 1)):
        return ["codex: bundles/index.json's file lists are not one per bundle, numbered from 1 (bundle again)"]
    listed = [str(f) for name in names if isinstance(lists[name], list) for f in lists[name]]
    if not isinstance(manifest, dict) or ai is None or not all(isinstance(lists[n], list) for n in names) or listed != [str(e.get("path")) for e in ai]:
        return ["codex: bundles/index.json's file lists are not the manifest's AI-readable files (bundle again)"]
    at = names.index(bundle_name)
    first = sum(len(lists[name]) for name in names[:at])
    try:  # bytes, not text: read_text would turn a staged file's CR LF or lone CR into LF
        expected: bytes | None = (
            _bundle_head(manifest, at + 1, len(names), len(files))
            + "".join(_bundle_block(run / "tree", e, first + k, len(ai)) for k, e in enumerate(ai[first : first + len(files)], 1))
        ).encode("utf-8")
    except (OSError, LookupError, ValueError):
        expected = None
    if expected is None or (run / "bundles" / bundle_name).read_bytes() != expected:
        return [f"codex: {bundle_name} is not what `bundle` writes for the files bundles/index.json lists (bundle again)"]
    return [f"codex: {p}" for p in answer_shape_problems(answer.name, got, files)]


def ab_findings(run: Path, upto: str = "codex") -> list[str]:
    """The ids of what needs clearing before the next vendor reads: every mechanical
    hit (`scan#<index>`, or all of a file's at once as `scan:<file>`); every reviewer
    finding (`<answer file>#<index>`) whose lens is not plainly C or D (a secret,
    personal data, internal information — or a lens labelled some other way: fail
    closed), and every critical or high one whatever its lens; and each answer's
    `not_read` list as a whole (`<answer file>#not_read`). ``upto`` "claude": the scan
    and Claude's answers (the gate before Codex); "codex": Codex's too (before Grok);
    "grok": also each Grok answer's `not_checked` (`<answer file>#not_checked`) and its
    findings, by the same rule (`stage4-grok-NN.json#<index>`: every one but a licence or
    harmful-instruction finding below high), at the seal — so a Grok finding the final
    read left out holds the seal. Only internal-information hits may be cleared a whole file at a time: a
    secret-shaped hit, a gitleaks hit, a private value or a sensitive file name is cleared
    by its own `scan#<index>`."""
    ids: list[str] = []
    scan = _json_file(run / "scan" / "report.json")
    for k, h in enumerate((scan.get("hits") if isinstance(scan, dict) else None) or []):
        kind = str(h.get("kind", "")).rsplit(":", 1)[-1] if isinstance(h, dict) else ""
        per_file = f"|scan:{h.get('file')}" if isinstance(h, dict) and kind in INFO_PATTERNS else ""
        ids.append(f"scan#{k}{per_file}")
    answers = [(p.name, _json_file(p)) for p in sorted(run.glob("stage2-claude*.json"))]
    if upto in ("codex", "grok"):
        answers += [(p.name, _last_json_block(p.read_text(encoding="utf-8", errors="replace"))) for p in sorted(run.glob("stage3-codex-*.md.codex.md"))]
    if upto == "grok":
        for p in sorted(run.glob("stage4-grok-*.json")):
            g = _json_file(p)
            if isinstance(g, dict) and str(g.get("not_checked") or "").strip():
                ids.append(f"{p.name}#not_checked")
            for k, f in enumerate(_list(g.get("findings") if isinstance(g, dict) else None)):
                category = str(f.get("category", "")).strip().lower() if isinstance(f, dict) else ""
                severe = not isinstance(f, dict) or str(f.get("severity", "")).strip().lower() not in ("medium", "low")
                if category not in ("licence", "license", "harmful-instruction") or severe:
                    ids.append(f"{p.name}#{k}")
    for name, a in answers:
        for k, f in enumerate(_list(a.get("findings") if isinstance(a, dict) else None)):
            lens = str(f.get("lens", "")).strip().upper() if isinstance(f, dict) else ""
            severe = isinstance(f, dict) and str(f.get("severity", "")).strip().lower() in ("critical", "high")
            if lens not in ("C", "D") or severe:
                ids.append(f"{name}#{k}")
        if isinstance(a, dict) and a.get("not_read"):
            ids.append(f"{name}#not_read")
    return ids


def _list(value: object) -> list[object]:
    """``value`` when it is a list, else an empty list (a malformed answer is caught by
    answer_shape_problems or the seal; it never crashes the gates)."""
    return value if isinstance(value, list) else []


def answer_shape_problems(name: str, answer: object, files: list[str]) -> list[str]:
    """A reviewer answer the gates can rely on: findings that each name a known
    severity and lens and a file, and a per-file coverage account — every one of the
    ``files`` it was given named in `read` (the paths it read) or in `not_read`
    ("<path>: <reason>"). One malformed finding, or a file the answer does not name,
    makes the stage incomplete, so nothing is skipped by a model's format slip or a
    read that stopped early. The account is the reviewer's own: it shows what the
    reviewer says it read, not that it read it (SKILL.md's threat model)."""
    if not isinstance(answer, dict) or not isinstance(answer.get("findings"), list):
        return [f"{name}: no findings list"]
    not_read = answer.get("not_read", [])
    read = answer.get("read")
    if not isinstance(not_read, list) or not all(isinstance(x, str) for x in not_read):
        return [f"{name}: not_read is not a list of \"<path>: <reason>\""]
    if not isinstance(read, list) or not all(isinstance(x, str) for x in read):
        return [f"{name}: no read list (the path of every file read)"]
    named = {x.strip() for x in read} | {x.split(": ", 1)[0].strip() for x in not_read}
    missing = [f for f in files if f not in named]
    if missing:
        return [f"{name}: {len(missing)} of the {len(files)} file(s) it was given named in neither read nor not_read (first: {safe(missing[0])}) — run the stage again"]
    for k, f in enumerate(answer["findings"]):
        if (
            not isinstance(f, dict)
            or str(f.get("severity", "")).strip().lower() not in ("critical", "high", "medium", "low")
            or not str(f.get("lens", "")).strip()
            or not str(f.get("file", "")).strip()
        ):
            return [f"{name}: finding #{k} lacks a known severity, a lens or a file"]
    return []


def clearance_problems(run: Path, digest: str, upto: str = "codex") -> list[str]:
    """`cleared.json` = {"digest", "bound": {<run-relative file>: <sha256>},
    "resolved": {<id>: <what was done>}}: every id ab_findings names up to that stage,
    each with a reason (later ids may be present already), and `bound` naming the
    scan report and every answer it was written for, unchanged since: a re-scan or a
    re-run answer leaves its old reasons unresolved."""
    cleared = _json_file(run / "cleared.json")
    resolved = cleared.get("resolved") if isinstance(cleared, dict) and cleared.get("digest") == digest else None
    bound = cleared.get("bound") if isinstance(cleared, dict) else None
    ids = ab_findings(run, upto)
    if not isinstance(resolved, dict) or not isinstance(bound, dict):
        return [f"cleared.json missing, for another digest, or not {{bound: {{file: sha256}}, resolved: {{id: reason}}}} ({len(ids)} item(s) to resolve)"]
    sources = [run / "scan" / "report.json", *sorted(run.glob("stage2-claude*.json"))]
    if upto in ("codex", "grok"):
        sources += sorted(run.glob("stage3-codex-*.md.codex.md"))
    if upto == "grok":
        sources += sorted(run.glob("stage4-grok-*.json"))
    stale = [s.relative_to(run).as_posix() for s in sources if s.is_file() and bound.get(s.relative_to(run).as_posix()) != sha256_file(s)]
    if stale:
        return [f"cleared.json was not written for the current {stale[0]} (bound hash missing or changed) — review its ids again"]

    def done(i: str) -> bool:  # "scan#3|scan:<file>": either key resolves an internal-information hit
        return any(str(resolved.get(alt) or "").strip() for alt in i.split("|"))

    missing = [i.split("|")[0] for i in ids if not done(i)]
    known = {alt for i in ab_findings(run, "grok") for alt in i.split("|")}
    unknown = [k for k in resolved if k not in known]
    if missing or unknown:
        return [f"cleared.json: unresolved {missing[:3]}, unknown {unknown[:3]}"]
    return []


def answers_hash(run: Path) -> str:
    """One hash over everything the verdict rests on: the scan, the triage, every stage's
    brief and answer, the clearance, the final read and the verdict. The seal records it."""
    names = ["scan/report.json", "triage.md", "cleared.json", "stage5-final.json", "verdict.json", "bundles/index.json"]
    files = sorted({*(run / n for n in names), *run.glob("stage2-claude*.json"), *run.glob("stage3-codex-*"), *run.glob("stage4-grok-*.json")})
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(run).as_posix().encode() + b"\0" + (sha256_file(f).encode() if f.is_file() else b"absent") + b"\n")
    return h.hexdigest()


def _json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _last_json_block(text: str) -> object:
    blocks = re.findall(r"```[ \t]*json[ \t]*\r?\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    try:
        return json.loads(blocks[-1]) if blocks else None
    except (ValueError, RecursionError):
        return None


# ── commands ───────────────────────────────────────────────────────────────────


def cmd_stage(out: Path, sources: list[Source]) -> int:
    home = Path.home().resolve()
    if out.resolve() == home or home in out.resolve().parents:
        die(f"--out {out}: under a home directory, which the Codex stage's read-deny profile hides — use a run directory outside every home (SKILL.md Step 0)")
    for s in sources:
        if s.kind == "tree" and (out.resolve() == s.path or s.path in out.resolve().parents or out.resolve() in s.path.parents):
            die(f"--out {out} and --tree {s.path} overlap: the run would stage itself")
    if out.exists() and any(out.iterdir()):
        die(f"--out {out}: not empty (one run directory per staging)")
    tree = out / "tree"
    tree.mkdir(parents=True)
    manifest = stage_into(sources, tree)
    manifest["sources"] = [{"kind": s.kind, "path": str(s.path), "remote": s.remote, "ref": s.ref, "whole": s.whole, "repo": s.repo} for s in sources]
    manifest["staged_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest["digest"] = digest_of(manifest)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "DIGEST").write_text(str(manifest["digest"]) + "\n")
    for p in sorted(tree.rglob("*"), reverse=True):  # read-only: a fix belongs in the source, then a new stage
        if not p.is_symlink():
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    entries = manifest["entries"]
    assert isinstance(entries, list)
    texts = sum(1 for e in entries if e["text"])
    ai = sum(1 for e in entries if e["ai"])
    print(f"STAGED {len(entries)} file(s): {texts} text ({ai} for the AI stages), {len(entries) - texts} binary")
    for e in entries:
        if e["text"] and not e["ai"]:
            print(f"MECHANICAL_ONLY {safe(str(e['path']))} ({e['size']:,} bytes: larger than {AI_MAX_BYTES:,})")
    for push in manifest["pushes"]:  # type: ignore[union-attr]
        print(f"PUSH {safe(push['url'])} {push['ref']} at {push['tip']}" + (" (whole history)" if push.get("whole_history") else ""))
    for ident in manifest["identities"]:  # type: ignore[union-attr]
        print(f"IDENTITY {safe(str(ident))} (public after the push)")
    print(f"DIGEST {manifest['digest']}")
    print(f"RUN {out}")
    return 0


def _redact(value: str) -> str:
    return f"{value[:4]}…({len(value)} chars)"


def _cut(text: str) -> str:
    """``text`` as a hit shows it: whole up to LABEL_MAX_CHARS, else its start with its
    length and a short hash, so two long texts that share a start stay apart."""
    if len(text) <= LABEL_MAX_CHARS:
        return text
    tag = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{text[: LABEL_MAX_CHARS - 48]}…[{len(text):,} chars, sha256 {tag}]"


# ── the values file (private values kept outside git) ─────────────────────────

#: The values file's one accepted place is default_values_path(): the seal takes a scan
#: of that file only, never of a --values-file trial (a file of one value would
#: otherwise do).
VALUES_DIR_NAME = "agrodesign-github-publish"
VALUES_FILE_NAME = "values.json"
VALUES_FORMAT = 1
VALUES_MAX_BYTES = 1 << 20
VALUE_MAX_CHARS = 1024
VALUE_MIN_SCANNED_CHARS = 3
VALUE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VALUE_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")


class ValuesError(Exception):
    """A values file that cannot be used: ``code`` is missing, unreadable, invalid or in-git."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def default_values_path() -> Path:
    """$XDG_CONFIG_HOME/agrodesign-github-publish/values.json, else ~/.config/agrodesign-github-publish/values.json."""
    raw = os.environ.get("XDG_CONFIG_HOME", "")
    base = raw if os.path.isabs(raw) else str(Path.home() / ".config")  # a relative value is ignored (XDG)
    return Path(base) / VALUES_DIR_NAME / VALUES_FILE_NAME


def _inside_git(path: Path) -> Path | None:
    """The top of a git work tree ``path`` lies in, or None: a values file there could be
    committed and published. Both the path as written and the path with every link
    resolved are checked (a linked config folder inside a dotfiles repository), and git
    itself is asked from the resolved folder (it also sees GIT_DIR / GIT_WORK_TREE set in
    the environment). A bare repository whose work tree is set only in its own config is
    invisible from here: reference/values-file.md says so."""
    for cand in (path, Path(os.path.realpath(path))):
        for parent in cand.parents:
            if (parent / ".git").exists():
                return parent
    folder = Path(os.path.realpath(path)).parent
    if folder.is_dir() and shutil.which("git"):
        got = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
        if got.returncode == 0 and got.stdout.strip():
            return Path(got.stdout.strip())
    return None


def _read_values_bytes(path: Path) -> bytes:
    """``path``'s bytes: a regular file opened without following a link, at most VALUES_MAX_BYTES."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise ValuesError("missing", f"{path} does not exist (reference/values-file.md)") from None
    except PermissionError:
        raise ValuesError("unreadable", f"{path}: this account may not read it") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValuesError("invalid", f"{path} is a symbolic link") from None
        raise ValuesError("unreadable", f"{path}: {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValuesError("invalid", f"{path} is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
            if sum(map(len, chunks)) > VALUES_MAX_BYTES:
                raise ValuesError("invalid", f"{path} is over {VALUES_MAX_BYTES} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise ValuesError("invalid", f"{k!r} appears twice")
        out[k] = v
    return out


def parse_values(data: bytes, where: str) -> list[tuple[str, str, bool]]:
    """(key, value, scanned) for each entry of a values file, or ValuesError("invalid").
    Never echoes a value."""
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError:
        raise ValuesError("invalid", f"{where} is not UTF-8") from None
    except ValueError as exc:
        raise ValuesError("invalid", f"{where} is not JSON (line {getattr(exc, 'lineno', '?')})") from None
    if not isinstance(doc, dict):
        raise ValuesError("invalid", f"{where}: the top level is not an object")
    unknown = sorted(k for k in doc if k not in ("format", "values") and not k.startswith("_"))
    if unknown:
        raise ValuesError("invalid", f"{where}: unknown field(s) {unknown} (a comment goes in a field starting with '_')")
    if type(doc.get("format")) is not int or doc.get("format") != VALUES_FORMAT:
        raise ValuesError("invalid", f"{where}: format must be {VALUES_FORMAT}")
    values = doc.get("values")
    if not isinstance(values, dict):
        raise ValuesError("invalid", f"{where}: values must be an object of key: value")
    out: list[tuple[str, str, bool]] = []
    for key, raw in values.items():
        if not VALUE_KEY.fullmatch(key):
            raise ValuesError("invalid", f"{where}: key {key!r} — lowercase letters, digits, '.', '_' or '-', at most 64")
        scan = True
        if isinstance(raw, dict):
            extra = sorted(set(raw) - {"value", "scan"})
            if extra:
                raise ValuesError("invalid", f"{where}: {key}: unknown field(s) {extra}")
            scan_raw = raw.get("scan", True)
            if not isinstance(scan_raw, bool):
                raise ValuesError("invalid", f"{where}: {key}: scan must be true or false")
            scan, raw = scan_raw, raw.get("value")
        if not isinstance(raw, str) or not raw:
            raise ValuesError("invalid", f"{where}: {key}: the value is not a non-empty string")
        if raw != raw.strip() or VALUE_CONTROL.search(raw):
            raise ValuesError("invalid", f"{where}: {key}: the value has surrounding blanks or a control character")
        if len(raw) > VALUE_MAX_CHARS:
            raise ValuesError("invalid", f"{where}: {key}: the value is over {VALUE_MAX_CHARS} characters")
        if scan and len(raw) < VALUE_MIN_SCANNED_CHARS:
            raise ValuesError("invalid", f"{where}: {key}: a scanned value needs {VALUE_MIN_SCANNED_CHARS}+ characters (or \"scan\": false)")
        out.append((key, raw, scan))
    return out


def load_values_file(path: Path) -> tuple[list[tuple[str, str]], str]:
    """The scanned (key, value) pairs of ``path`` and the sha256 of the very bytes they came
    from (one read), or ValuesError. A file inside a git work tree is refused."""
    real = Path(os.path.abspath(path))
    top = _inside_git(real)
    if top is not None:
        raise ValuesError("in-git", f"{real} lies inside the git work tree {top}: keep it outside every work tree, where it can never be committed")
    data = _read_values_bytes(real)
    entries = parse_values(data, str(real))
    return [(k, v) for k, v, scan in entries if scan], hashlib.sha256(data).hexdigest()


def _load_values(path: Path | None) -> tuple[list[tuple[str, str]], dict[str, object]]:
    """The (key, value) pairs to match and what the report records: the source, its sha256
    and the count, which values_seal_problem compares at seal, verify and push. A
    --values-file that fails is a usage error; the default file that fails is recorded, the
    generic patterns still run, and seal refuses the run."""
    source = default_values_path() if path is None else path
    try:
        pairs, sha = load_values_file(source)
    except ValuesError as exc:
        if path is not None:
            die(f"--values-file {path}: VALUES {exc.code}: {exc.detail}")
        return [], {"values": "missing", "values_problem": f"{exc.code}: {exc.detail}"}
    return pairs, {
        "values": "checked" if pairs else "empty",
        "values_source": str(Path(os.path.abspath(source))),
        "values_sha256": sha,
        "value_count": len(pairs),
    }


def values_seal_problem(scan: object) -> str | None:
    """Why a run may not be sealed on ``scan``'s private values (None: it may). The scan
    must have read THE values file — default_values_path(), never a --values-file trial —
    holding at least one scanned value, and the file must still be the bytes it read (a
    value added since would go out unchecked)."""
    if not isinstance(scan, dict) or scan.get("values") != "checked":
        why = (scan.get("values_problem") or scan.get("values") or "no scan report") if isinstance(scan, dict) else "no scan report"
        return f"private values were not checked ({why}): write your values file (reference/values-file.md) with at least one value and scan again"
    real = str(Path(os.path.abspath(default_values_path())))
    if scan.get("values_source") != real:
        return f"private values were checked from {scan.get('values_source')}, not the values file {real}: scan again without --values-file"
    try:
        _pairs, sha = load_values_file(Path(real))
    except ValuesError as exc:
        return f"private values: the values file cannot be read now ({exc.code}: {exc.detail}); scan again once it can"
    if scan.get("values_sha256") != sha:
        return "private values changed since the scan: scan again"
    return None


class Scanner:
    def __init__(self, values: list[tuple[str, str]]) -> None:
        self.lowered = [(k, v.lower()) for k, v in values]
        self.hits: list[dict[str, object]] = []
        self.seen: dict[tuple[str, str, str], dict[str, object]] = {}

    def hit(self, file: str, line: int, kind: str, show: str, value: str | None = None) -> None:
        """Record a hit. Repeats of one value (``value``, else what is shown) of one kind in
        one file are one hit with a `count`, its first line kept and up to MORE_LINES_MAX
        further lines listed in `more_lines`: a payload that expands into millions of copies
        holds one entry, and its clearance covers every copy. Every distinct value is
        listed; past MAX_HITS_TOTAL distinct hits the scan stops (ScanTooLarge) and nothing
        passes. The file label and what is shown are cut to LABEL_MAX_CHARS (a 64 KB
        archive member name would otherwise repeat in every hit of that member); the key
        keeps them whole, so two long names stay two hits."""
        key = (file, kind, hashlib.sha256((show if value is None else value).encode("utf-8", "surrogatepass")).hexdigest())
        got = self.seen.get(key)
        if got is not None:
            got["count"] = int(str(got.get("count", 1))) + 1
            more = got.get("more_lines")
            more = more if isinstance(more, list) else []
            if len(more) < MORE_LINES_MAX and line != got["line"] and line not in more:
                got["more_lines"] = [*more, line]
            return
        if len(self.hits) >= MAX_HITS_TOTAL:
            raise ScanTooLarge(f"over {MAX_HITS_TOTAL:,} distinct hits")
        entry: dict[str, object] = {"file": _cut(safe(file)), "line": line, "kind": kind, "show": _cut(show)}
        self.seen[key] = entry
        self.hits.append(entry)

    def name(self, file: str, name: str) -> None:
        if SENSITIVE_NAMES.search(name):
            self.hit(file, 0, "sensitive_file_name", safe(name))
        self.text(file, name, "name:", 1)  # a path can carry a secret, a host name or a private value too

    def text(self, file: str, text: str, prefix: str = "", depth: int = 0) -> None:
        for lineno, line in enumerate(text.splitlines(), 1):
            masked: list[tuple[int, int]] = []  # secret and private-value spans: nothing inside them prints
            for kind, rx in SECRET_PATTERNS.items():
                for m in rx.finditer(line):
                    masked.append(m.span())
                    self.hit(file, lineno, prefix + kind, _redact(m.group(0)), m.group(0))
            if self.lowered:
                low = line.lower()
                for key, v in self.lowered:
                    start = low.find(v)
                    if start >= 0:
                        self.hit(file, lineno, prefix + "private_value", f"value {key}")
                    while start >= 0:  # every occurrence is masked, not only the first
                        masked.append((start, start + len(v)))
                        start = low.find(v, start + 1)
            for kind, rx in INFO_PATTERNS.items():
                for m in rx.finditer(line):
                    if kind == "email" and PUBLIC_EMAILS.search(m.group(0)):
                        continue
                    hidden = any(m.start() < e and s < m.end() for s, e in masked)
                    self.hit(file, lineno, prefix + kind, _redact(m.group(0)) + " (overlaps a secret)" if hidden else m.group(0), m.group(0))
            if depth == 0:
                for m in BASE64_RUN.finditer(line):
                    decoded, raw, complete = decode_blob(m.group(0))
                    if decoded is not None:
                        self.text(f"{file}:{lineno}", decoded, prefix + "decoded:", depth + 1)
                    elif raw is not None:  # archives opened, other binaries by their strings
                        self.binary(f"{file}:{lineno}", raw, depth + 1)
                    if not complete:
                        self.hit(file, lineno, prefix + "decoded_incomplete", "a gzip payload over 60 MB or with trailing data: not fully scanned")

    def binary(self, file: str, data: bytes, depth: int = 0) -> None:
        """Archive members by name and content; other binaries by their printable strings.
        ``depth`` 1 for a payload decoded out of a text file: its texts are not decoded again.
        A lone .gz / .xz / .bz2 / .lzma / .zst is decompressed under DECODED_MAX_BYTES first,
        and what comes out is tried as an archive once more (a .tar.gz's members), never
        deeper: a compressed stream inside it is named. A format the scan cannot open (7z,
        RAR, LZ4, lzip, zstd on a Python without it) is named as unread, not read as
        printable strings, which show nothing of a compressed body."""
        if self._archive(file, data, depth):
            return
        single, complete, side = decompress_single(data)  # a lone .gz / .bz2 / .xz / .lzma / .zst file (a dump, a JSON asset)
        if not complete:
            self.hit(file, 0, "decompressed_incomplete", "cut, over 60 MB, or with trailing data: not fully scanned")
        if side:  # zstd skippable frames: plain bytes beside the content
            self.text(file, "\n".join(m.group(0).decode("ascii") for m in PRINTABLE_RUN.finditer(side)), "zstd-skippable:", depth)
        kind = compressed_kind(data)
        if single is None and (kind in UNREAD_KINDS or (kind == "zstd" and _zstd is None)):
            what = UNREAD_KINDS.get(str(kind), "zstd (Python 3.14 or later reads it)")
            self.hit(file, 0, "compressed_unread", f"a {what} archive or stream this scan cannot open: not scanned; unpack it and stage its contents as files")
            return
        if single is not None:
            if self._archive(file, single, depth):  # a compressed tar or zip: its members, once
                return
            inner = compressed_kind(single)
            if inner is not None:  # a stream in a stream (.xz.gz): one level only, so named
                self.hit(file, 0, "nested_archive", f"a {UNREAD_KINDS.get(inner, inner)} stream inside the decompressed file: open it and stage its contents as files")
                return
            decoded = strict_decode(single) if b"\0" not in single else None
            if decoded is not None:
                self.text(file, decoded, "decompressed:", depth)
            else:
                self.text(file, "\n".join(m.group(0).decode("ascii") for m in PRINTABLE_RUN.finditer(single)), "decompressed-binary:", depth)
            return
        strings = "\n".join(m.group(0).decode("ascii") for m in PRINTABLE_RUN.finditer(data))
        self.text(file, strings, "binary:", depth)

    def _archive(self, file: str, data: bytes, depth: int) -> bool:
        """Scan ``data``'s members if it is an archive; whether it was one. A tar is tried
        first, uncompressed only and whatever its header format (a tar whose last member is
        a zip also passes is_zipfile, and a v7 tar has no ustar marker); then, whether or
        not a tar opened (a zip with a tar in front of it), a zip, its directory entries
        counted (zip_directory_entries) before it is parsed. Each pass has its own member
        and byte budget. Members are read and scanned one at a time, so memory holds one
        member's content at most."""
        tar_seen, zip_seen = [0], [0]  # members scanned so far by each pass, kept when it breaks part way
        broken = (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, ValueError, RuntimeError, NotImplementedError, zlib.error, lzma.LZMAError)
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                self._members(file, ((ti.name, _tar_reader(tf, ti)) for ti in tf), depth, tar_seen)
        except broken:
            if tar_seen[0]:  # opened as a tar, then broke part way: what was not read is named
                self.hit(file, 0, "archive_unreadable", "the archive broke part way: the members after the last one listed were not read")
                return True
        if not zipfile.is_zipfile(io.BytesIO(data)):  # a zip with a tar in front of it: both member sets are scanned
            return tar_seen[0] > 0
        if zip_directory_entries(data) > ARCHIVE_MAX_MEMBERS:
            raise ScanTooLarge(f"{safe(file)}: over {ARCHIVE_MAX_MEMBERS:,} archive entries in its directory")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                self._members(file, ((i.filename, (lambda: b"") if i.is_dir() else (lambda i=i: zf.read(i)) if i.file_size <= MEMBER_MAX_BYTES else None) for i in zf.infolist()), depth, zip_seen)
        except broken:
            if zip_seen[0]:  # the zip broke part way: what was not read is named
                self.hit(file, 0, "archive_unreadable", "the archive broke part way: the members after the last one listed were not read")
        return tar_seen[0] + zip_seen[0] > 0

    def _members(self, file: str, items: Iterator[tuple[str, Callable[[], bytes] | None]], depth: int, seen: list[int]) -> None:
        """Scan an archive's members as they are read, counting them in ``seen[0]``. Past
        ARCHIVE_MAX_MEMBERS members or ARCHIVE_MAX_BYTES read in all the scan stops
        (ScanTooLarge): no report, so nothing passes on a partial read."""
        total = 0
        for name, reader in items:
            seen[0] += 1
            if seen[0] > ARCHIVE_MAX_MEMBERS:
                raise ScanTooLarge(f"{safe(file)}: over {ARCHIVE_MAX_MEMBERS:,} archive members")
            body = None
            if reader is not None:
                try:
                    body = reader()
                except (RuntimeError, NotImplementedError, zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError):
                    body = None  # encrypted, an unsupported method or a broken stream: named as unread
            total += len(body) if body else 0
            if total > ARCHIVE_MAX_BYTES:
                raise ScanTooLarge(f"{safe(file)}: over {ARCHIVE_MAX_BYTES:,} bytes of archive members")
            self._member(file, name, body, depth)

    def _member(self, file: str, name: str, body: bytes | None, depth: int) -> None:
        """One archive member: its name, then its content as text or printable strings. A
        member that is itself an archive or a compressed stream (a tar included) is named
        nested_archive, as the top level names a stream inside a decompressed file."""
        label = f"{file}!{name}"
        self.name(label, name.rsplit("/", 1)[-1])
        self.text(label, name, "archive-name:", 1)  # a member's path can carry a host name or a private value too
        if body is None:
            self.hit(label, 0, "archive_member_unread", "a member over 5 MB, or one that could not be read (encrypted, an unsupported method, a device)")
        elif compressed_kind(body) is not None or zipfile.is_zipfile(io.BytesIO(body)) or opens_as_tar(body):  # a .zip, .tar, .tar.gz, .xz, .7z … member
            # Named, never opened: an archive inside an archive is read by no pass, so a
            # compressed stream inside it (a zip's inner.tar holding x.gz) would give no hit
            # if the member were read as printable strings.
            self.hit(label, 0, "nested_archive", "open it and stage its contents as files")
        else:
            decoded = strict_decode(body) if b"\0" not in body or body.startswith((b"\xff\xfe", b"\xfe\xff")) else None
            if decoded is not None:
                self.text(label, decoded, "archive:", depth)
            else:  # a binary member: its printable strings, as for a binary file
                self.text(label, "\n".join(m.group(0).decode("ascii") for m in PRINTABLE_RUN.finditer(body)), "archive-binary:", depth)


class ScanTooLarge(Exception):
    """More distinct hits, or a larger archive, than one scan holds: the scan stops, and no report is valid."""


def cmd_scan(run: Path, values_file: Path | None) -> int:
    try:
        return _scan(run, values_file)
    except ScanTooLarge as exc:  # fail closed: no report, so no gate opens and nothing is sealed
        (run / "scan" / "report.json").unlink(missing_ok=True)
        print(f"SCAN=too-large {exc} — no report written; split the set or take the noisy or oversized files out, then scan again")
        print("RESULT=HITS")
        return 1


def _scan(run: Path, values_file: Path | None) -> int:
    manifest = checked_manifest(run)
    tree = run / "tree"
    scan_dir = run / "scan"
    scan_dir.mkdir(exist_ok=True)
    values, site = _load_values(values_file)
    sc = Scanner(values)
    binaries: list[dict[str, object]] = []
    for e in manifest["entries"]:  # type: ignore[union-attr]
        rel = str(e["path"])
        # Only the staging suffix ("~<id>") or collision prefix ("<id>~") comes off; an
        # editor backup ("id_rsa~", ".env~") is checked with and without its tilde.
        base = re.sub(r"^[0-9a-f]{40,64}~", "", re.sub(r"~[0-9a-f]{40,64}$", "", rel.rsplit("/", 1)[-1]))
        sc.name(rel, base)
        if base.endswith("~") and SENSITIVE_NAMES.search(base[:-1]):
            sc.hit(rel, 0, "sensitive_file_name", safe(base))
        target = e.get("symlink_target")
        if target and (str(target).startswith("/") or ".." in Path(str(target)).parts):
            sc.hit(rel, 0, "symlink_outside", safe(str(target)))
        if target:
            sc.text(rel, str(target), "link:", 1)
        if rel.endswith(".gitattributes") and e["text"] and "filter=lfs" in read_text(tree / rel, str(e["encoding"])):
            sc.hit(rel, 0, "git_lfs", "LFS objects are not staged: refuse, or stage them with git lfs")
        if not e["text"]:
            binaries.append({"file": rel, "size": e["size"], "sha256": e["sha256"]})
            sc.binary(rel, (tree / rel).read_bytes())
            continue
        body = read_text(tree / rel, str(e["encoding"]))
        if body.startswith(LFS_POINTER.decode()):
            sc.hit(rel, 1, "git_lfs_pointer", "an LFS pointer: its object is not in the set")
        sc.text(rel, body)
        if str(e["encoding"]) not in ("utf-8", "utf-16"):
            raw_ascii = "\n".join(m.group(0).decode("ascii") for m in PRINTABLE_RUN.finditer((tree / rel).read_bytes()))
            sc.text(rel, raw_ascii, "raw-ascii:", 1)
    if not GITLEAKS or not os.access(GITLEAKS, os.X_OK):
        print("GITLEAKS=missing — install gitleaks (or set AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN); no scan result is valid without it", file=sys.stderr)
        return 3
    report = scan_dir / "gitleaks.json"
    # gitleaks' own rules, whatever the staged tree or the environment says: an explicit
    # config that only extends the defaults, an empty ignore file, a clean cwd and env
    # (a .gitleaks.toml or .gitleaksignore in the set, or GITLEAKS_CONFIG, would weaken it).
    with tempfile.TemporaryDirectory(prefix="agrodesign-github-publish-gitleaks-") as gl_tmp:
        conf, ignore = Path(gl_tmp) / "gitleaks.toml", Path(gl_tmp) / "ignore"
        conf.write_text("[extend]\nuseDefault = true\n")
        ignore.mkdir()
        (ignore / ".gitleaksignore").write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS")}
        proc = subprocess.run(
            [GITLEAKS, "dir", str(tree), "--no-banner", "--redact", "--exit-code", "0", "--max-archive-depth", "4",  # the account's own gitleaks (PATH or AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN), argv a list, no shell
             "--ignore-gitleaks-allow", "--config", str(conf), "--gitleaks-ignore-path", str(ignore),
             "--report-format", "json", "--report-path", str(report)],
            capture_output=True, check=False, cwd=gl_tmp, env=env,
        )  # fmt: skip
        version = subprocess.run([GITLEAKS, "version"], capture_output=True, text=True, check=False, env=env).stdout.strip()  # the account's own gitleaks (PATH or AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN), argv a list, no shell
    try:
        raw = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    except ValueError:
        raw = None
    if proc.returncode != 0 or not isinstance(raw, list):
        print(f"GITLEAKS=failed rc={proc.returncode}: {proc.stderr.decode(errors='replace').strip()[-300:]}", file=sys.stderr)
        return 3
    for f in raw:
        if isinstance(f, dict):
            file = str(f.get("File") or "")
            rel = os.path.relpath(file, tree) if os.path.isabs(file) else file
            line = int(f.get("StartLine") or 0)
            sc.hit(rel, line, f"gitleaks:{f.get('RuleID', '?')}", "(redacted by gitleaks)", f"{line}:{f.get('StartColumn')}:{f.get('Fingerprint')}")
    hits = sc.hits
    result = {
        "digest": manifest.get("digest"),
        **site,
        "gitleaks": {"version": version, "rules": "default (useDefault), no allow comments, no ignore file"},
        "hits": hits,
        "binaries": binaries,
        "identities": manifest.get("identities", []),
        "pushes": manifest.get("pushes", []),
    }
    (scan_dir / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    by_kind: dict[str, int] = {}
    for h in hits:
        by_kind[str(h["kind"])] = by_kind.get(str(h["kind"]), 0) + 1
    print(f"SCAN digest {manifest.get('digest')}")
    why = f" ({site['values_problem']})" if "values_problem" in site else ""
    trial = "" if values_file is None else " — a --values-file trial; seal refuses this run"
    tail = f" ({len(values)} value(s) from {site['values_source']}){trial}" if values else f"{why} — generic patterns only; seal refuses this run"
    print(f"VALUES {site['values']}{tail}")
    for kind, count in sorted(by_kind.items()):
        print(f"HITS {kind} {count}")
    for h in hits[:MAX_HITS_SHOWN]:
        more = h.get("more_lines") or []
        also = f", also line(s) {', '.join(str(n) for n in more)}" + ("…" if len(more) >= MORE_LINES_MAX else "") if more else ""
        print(f"HIT {h['file']}:{h['line']} {h['kind']} {h['show']}" + (f" (x{h['count']}{also})" if "count" in h else ""))
    if len(hits) > MAX_HITS_SHOWN:
        print(f"HIT … {len(hits) - MAX_HITS_SHOWN} more in {scan_dir / 'report.json'}")
    for b in binaries:
        print(f"BINARY {safe(str(b['file']))} {b['size']:,} bytes sha256 {str(b['sha256'])[:16]}")
    print(f"REPORT {scan_dir / 'report.json'}")
    print("RESULT=" + ("HITS" if hits else "CLEAN"))
    return 1 if hits else 0


def _bundle_head(manifest: dict[str, object], n: int, total: int, count: int) -> str:
    """Bundle ``n`` of ``total``'s three head lines, as cmd_bundle writes them."""
    entries = [e for e in manifest["entries"] if isinstance(e, dict)]  # type: ignore[attr-defined]
    skipped = [str(e["path"]) for e in entries if not e["ai"]]
    return (
        f"# Upload set {manifest.get('digest')} — bundle {n} of {total}\n"
        f"# {count} file(s) in this bundle; {sum(1 for e in entries if e['ai'])} text file(s) in all.\n"
        f"# Not in any bundle (binary or larger than {AI_MAX_BYTES:,} bytes; scanned mechanically only): " + (", ".join(skipped) or "none") + "\n"
    )


def _bundle_block(tree: Path, entry: dict[str, object], k: int, total: int) -> str:
    """The ``k``-th of ``total`` AI-readable files as a bundle holds it: its header line, then its whole text."""
    body = read_text(tree / str(entry["path"]), str(entry["encoding"]))
    return f"\n===== FILE {k}/{total}: {entry['path']} (sha256 {str(entry['sha256'])[:16]}, {entry['size']:,} bytes) =====\n{body}\n"


def _bundle_number(name: str) -> int:
    """bundle-NN.md's number (-1 for a name of another shape), so bundle-100 sorts after bundle-99."""
    nn = name.removeprefix("bundle-").removesuffix(".md")
    return int(nn) if nn.isdigit() and name == f"bundle-{nn}.md" else -1


def cmd_bundle(run: Path, max_bytes: int) -> int:
    manifest = checked_manifest(run)
    tree = run / "tree"
    picked = [e for e in manifest["entries"] if e["ai"]]  # type: ignore[union-attr]
    out_dir = run / "bundles"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()
    bundles: list[list[str]] = [[]]
    paths: list[list[str]] = [[]]
    size = 0
    for k, e in enumerate(picked, 1):
        block = _bundle_block(tree, e, k, len(picked))
        if size and size + len(block.encode()) > max_bytes:
            bundles.append([])
            paths.append([])
            size = 0
        bundles[-1].append(block)
        paths[-1].append(str(e["path"]))
        size += len(block.encode())
    total = len(bundles)
    index: dict[str, str] = {}
    files: dict[str, list[str]] = {}
    for n, blocks in enumerate(bundles, 1):
        path = out_dir / f"bundle-{n:02d}.md"
        path.write_text(_bundle_head(manifest, n, total, len(blocks)) + "".join(blocks), encoding="utf-8")
        index[path.name] = sha256_file(path)
        files[path.name] = paths[n - 1]
        print(f"BUNDLE {path} {len(blocks)} file(s) {path.stat().st_size:,} bytes")
    # What the Codex and Grok stages must read, bound to the digest (grok_review.py checks it before sending).
    # `files`: the paths each bundle holds, which a Codex answer's read / not_read must name.
    (out_dir / "index.json").write_text(json.dumps({"digest": manifest.get("digest"), "bundles": index, "files": files}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def gate_problems(run: Path, before: str) -> list[str]:
    """Why the set may not go to the next vendor yet: before Codex, stages 1-2 for this
    digest (the staged tree intact, the scan and its triage, Claude's answers) and
    Claude's findings cleared; before Grok, stages 1-3 and Claude's and Codex's."""
    manifest = checked_manifest(run)
    digest = str(manifest.get("digest"))
    upto = "claude" if before == "codex" else "codex"
    return stage_problems(run, manifest, {}, upto=upto) + clearance_problems(run, digest, upto)


def cmd_gate(run: Path, before: str) -> int:
    problems = gate_problems(run, before)
    for p in problems:
        print(f"GATE=closed before {before}: {p}")
    if not problems:
        print(f"GATE=open before {before} {(run / 'DIGEST').read_text().strip()}")
    return 1 if problems else 0


def cmd_seal(run: Path) -> int:
    problems = seal_problems(run)
    sealed = run / "SEALED"
    if problems:
        sealed.unlink(missing_ok=True)  # a refused seal withdraws an earlier one
        for p in problems:
            print(f"SEAL=refused: {p}")
        return 1
    digest = (run / "DIGEST").read_text().strip()
    sealed.write_text(f"{digest} {answers_hash(run)} {datetime.now().astimezone().isoformat(timespec='seconds')}\n")
    print(f"SEAL=ok {digest}")
    return 0


def seal_problems(run: Path) -> list[str]:
    """Why the run may not be sealed (empty: it may). verify and push ask again, so a
    verdict or a stage answer changed after the seal withdraws it."""
    manifest = checked_manifest(run)
    digest = str(manifest.get("digest"))
    try:
        verdict = json.loads((run / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["verdict.json missing or not JSON"]
    problems: list[str] = []
    if not isinstance(verdict, dict):
        problems.append("verdict.json is not an object")
        verdict = {}
    if verdict.get("digest") != digest:
        problems.append(f"verdict digest {verdict.get('digest')!r} is not the staged {digest}")
    if verdict.get("verdict") != "PUBLISH_OK":
        problems.append(f"verdict is {verdict.get('verdict')!r}, not PUBLISH_OK")
    stages = verdict.get("stages") if isinstance(verdict.get("stages"), dict) else {}
    assert isinstance(stages, dict)
    for name in STAGES:
        st = stages.get(name)
        status = st.get("status") if isinstance(st, dict) else None
        if status == "done" or (status == "waived" and isinstance(st, dict) and str(st.get("owner_words") or "").strip()):
            continue
        problems.append(f"stage {name}: {status!r} (needs done, or waived with the owner's words)")
    problems += stage_problems(run, manifest, stages)
    problems += clearance_problems(run, digest, "grok")  # scan hits are cleared whatever stage was waived
    try:
        scan = json.loads((run / "scan" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        scan = {}
    site = values_seal_problem(scan)  # no owner's-words waiver: the values are the scan's own check
    if site:
        problems.append(site)
    return problems


def sealed_state(run: Path) -> tuple[str | None, str]:
    """(digest, "") while the run's seal holds; (None, why) when it does not."""
    sealed = run / "SEALED"
    if not sealed.exists():
        return None, "this run has no seal — finish the checks first"
    sealed_digest, sealed_answers = (sealed.read_text().split() + ["", ""])[:2]
    problems = seal_problems(run)  # also proves the staged copies (release title, notes, assets) unchanged
    if problems or answers_hash(run) != sealed_answers:
        return None, problems[0] if problems else "a scan, answer, clearance or verdict changed after the seal"
    return sealed_digest, ""


def _same(run: Path, sources: list[Source]) -> tuple[bool, dict[str, object] | None]:
    digest, why = sealed_state(run)
    if digest is None:
        print(f"VERIFY=unsealed: {why}" if not (run / "SEALED").exists() else f"VERIFY=withdrawn: the seal no longer holds ({why})")
        return False, None
    recorded = load_manifest(run).get("pushes")
    with tempfile.TemporaryDirectory(prefix="agrodesign-github-publish-verify-") as tmp:
        manifest = stage_into(sources, Path(tmp), recorded if isinstance(recorded, list) else None)
    now = digest_of(manifest)
    if now != digest:
        print(f"VERIFY=different: sealed {digest}, now {now} — stage and check again")
        return False, None
    print(f"VERIFY=same {now}")
    return True, manifest


def cmd_verify(run: Path, sources: list[Source]) -> int:
    return 0 if _same(run, sources)[0] else 1


# ── GitHub: the destination, read and changed through the gh CLI ───────────────


class GhError(Exception):
    """A GitHub read or change that did not succeed (never taken as "empty")."""


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    if not GH:
        die("gh (GitHub CLI) missing: install it and log in with access to the destination", 3)
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "NO_COLOR": "1"}
    return subprocess.run([GH, *args], capture_output=True, text=True, check=False, env=env)


def gh_api(path: str, *, missing_ok: bool = False) -> object:
    """One REST read. HTTP 404 is None only where the caller asks (a Pages site, a tag);
    every other failure raises, so an unreadable list is never mistaken for an empty one."""
    proc = gh("api", "-H", "Accept: application/vnd.github+json", path)
    if proc.returncode != 0:
        err = (proc.stderr.strip() or proc.stdout.strip())
        if missing_ok and "(HTTP 404)" in err:
            return None
        raise GhError(f"gh api {path}: {safe(err[-300:])}")
    try:
        return json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise GhError(f"gh api {path}: not JSON") from exc


def gh_list(path: str) -> list[object]:
    """Every page of a list endpoint (per_page=100), or GhError."""
    sep = "&" if "?" in path else "?"
    items: list[object] = []
    for page in range(1, MAX_PAGES + 1):
        got = gh_api(f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(got, list):
            raise GhError(f"gh api {path}: not a list")
        items.extend(got)
        if len(got) < PAGE_SIZE:
            return items
    raise GhError(f"gh api {path}: more than {MAX_PAGES} pages")


def gh_total(path: str) -> int:
    got = gh_api(path)
    if not isinstance(got, dict) or not isinstance(got.get("total_count"), int):
        raise GhError(f"gh api {path}: no total_count")
    return int(got["total_count"])


def gh_repo(name: str) -> dict[str, Any]:
    got = gh_api(f"repos/{name}")
    if not isinstance(got, dict) or not isinstance(got.get("id"), int):
        raise GhError(f"gh api repos/{name}: not a repository")
    return got


def public_meta(meta: dict[str, Any]) -> dict[str, object]:
    """What a visitor reads about the repository beside its files."""
    return {"full_name": meta.get("full_name"), "description": meta.get("description"), "homepage": meta.get("homepage"), "topics": sorted(meta.get("topics") or [])}


def norm_url(url: str) -> str:
    """One spelling for a repository URL: https, no user, no trailing .git or slash, lower case."""
    u = url.strip()
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", u)
    if m:
        u = f"https://{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^git://", "https://", u).rstrip("/")
    return (u[:-4] if u.endswith(".git") else u).lower()


def repo_urls(meta: dict[str, Any]) -> set[str]:
    return {norm_url(str(meta[k])) for k in ("clone_url", "ssh_url", "git_url", "html_url") if meta.get(k)}


def _now() -> datetime:
    return datetime.now().astimezone()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── approvals: the owner's words, bound to what they approved, used once ────────


def approval_file(run: Path, step: str) -> Path:
    return run / "approvals" / f"{step}.json"


def push_rows(manifest: dict[str, object]) -> list[dict[str, str]]:
    return [{"url": str(p["url"]), "ref": str(p["ref"]), "tip": str(p["tip"])} for p in manifest.get("pushes") or []]  # type: ignore[union-attr]


def approval_expired(rec: dict[str, Any]) -> str | None:
    try:
        expires = datetime.fromisoformat(str(rec.get("expires_at")))
    except ValueError:
        return "has no valid expiry"
    return f"expired at {rec.get('expires_at')}" if _now() >= expires else None


def approval_closed(rec: dict[str, Any]) -> str | None:
    """Why an approval can no longer be used (None while it can)."""
    if isinstance(rec.get("revoked"), dict):
        return f"revoked at {rec['revoked'].get('at')}"
    if isinstance(rec.get("used"), dict):
        return f"used at {rec['used'].get('at')} ({rec['used'].get('result')})"
    return approval_expired(rec)


def approval_problems(run: Path, step: str, digest: str, meta: dict[str, Any], *, used_ok: str | None = None) -> list[str]:
    """Why the recorded approval for ``step`` does not cover this action (empty: it does)."""
    rec = _json_file(approval_file(run, step))
    if not isinstance(rec, dict):
        return [f"no {step} approval: record the owner's words first (approve --step {step})"]
    problems: list[str] = []
    if rec.get("format") != 1 or rec.get("step") != step or not str(rec.get("owner_words") or "").strip():
        problems.append(f"approvals/{step}.json is malformed or holds no owner's words")
    if rec.get("digest") != digest:
        problems.append(f"the approval is for digest {rec.get('digest')}, the run is {digest}")
    if rec.get("seal") != sha256_file(run / "SEALED"):
        problems.append("the seal changed since the approval")
    repo = rec.get("repo") if isinstance(rec.get("repo"), dict) else {}
    assert isinstance(repo, dict)
    if repo.get("id") != meta.get("id") or str(repo.get("full_name", "")).lower() != str(meta.get("full_name", "")).lower():
        problems.append(f"the approval is for {repo.get('full_name')} (id {repo.get('id')}), the destination is {meta.get('full_name')} (id {meta.get('id')})")
    if rec.get("pushes") != push_rows(load_manifest(run)):
        problems.append("the approval names other pushes than the run")
    revoked = rec.get("revoked") if isinstance(rec.get("revoked"), dict) else None
    used = rec.get("used") if isinstance(rec.get("used"), dict) else None
    expired = approval_expired(rec)
    if revoked:
        problems.append(f"the {step} approval was revoked at {revoked.get('at')}")
    if expired:
        problems.append(f"the {step} approval {expired}")
    if used and not (used_ok and used.get("result") == used_ok):  # a publish approval covers its one release after the switch
        problems.append(f"the {step} approval was used at {used.get('at')} ({used.get('result')})")
    return problems


def mark_used(run: Path, step: str, result: str) -> None:
    path = approval_file(run, step)
    rec = _json_file(path)
    if isinstance(rec, dict):
        rec["used"] = {"at": _now().isoformat(timespec="seconds"), "result": result}
        _write_json(path, rec)


def staged_file(manifest: dict[str, object], path: Path, what: str) -> tuple[str, str]:
    """(sha256, staged path) of a release file: one of this run's --file entries, with the
    same bytes and the same name (the name is what a release asset is published as)."""
    if not path.is_file():
        die(f"{what} {path}: not a file")
    digest = sha256_file(path)
    for e in manifest["entries"]:  # type: ignore[union-attr]
        if e.get("origin") == f"file:{path.name}" and e.get("sha256") == digest:
            return digest, str(e["path"])
    die(f"{what} {path}: not a staged --file of this run under that name (stage the release title, notes and assets with the push)")
    raise AssertionError


def release_spec(manifest: dict[str, object], tag: str, target: str, title: Path, notes: Path, assets: list[Path]) -> dict[str, object]:
    if not TAG_NAME.fullmatch(tag) or ".." in tag or tag.endswith((".", "/", ".lock")):
        die(f"--release-tag {tag!r}: not a plain tag name")
    commits = {str(p.get("commit")) for p in manifest.get("pushes") or []}  # type: ignore[union-attr]
    if not FULL_SHA.fullmatch(target) or target not in commits:
        die(f"--release-target {target!r}: give the full commit id this run pushes ({', '.join(sorted(commits)) or 'none'})")
    text = title.read_text(encoding="utf-8").strip() if title.is_file() else ""
    if not text or "\n" in text:
        die(f"--release-title {title}: one non-empty line")
    names = [a.name for a in assets]
    odd = [n for n in names if not ASSET_NAME.fullmatch(n)]
    if odd:  # gh reads "name#label", and GitHub rewrites spaces and some characters in asset names
        die(f"--release-asset: rename {', '.join(odd)} to letters, digits, '.', '_' and '-' only (GitHub would publish another name)")
    if len(set(names)) != len(names):
        die("--release-asset: two assets share a name")
    title_sha, title_path = staged_file(manifest, title, "--release-title")
    notes_sha, notes_path = staged_file(manifest, notes, "--release-notes")
    staged_assets = [(a.name, *staged_file(manifest, a, "--release-asset")) for a in assets]
    return {"tag": tag, "target": target, "title_sha256": title_sha, "title_path": title_path, "notes_sha256": notes_sha, "notes_path": notes_path,
            "assets": [{"name": n, "sha256": s, "path": p} for n, s, p in staged_assets]}  # fmt: skip


def release_allowed(state: dict[str, Any], meta: dict[str, Any]) -> bool:
    """A release with its own approval: the repository is public and this run either made it
    public or pushed its checked commit there (a later version of a public repository)."""
    def by_run(key: str) -> bool:
        return isinstance(state.get(key), dict) and state[key].get("repo_id") == meta.get("id")

    return by_run("published") or (meta.get("private") is False and by_run("pushed"))


def cmd_approve(run: Path, step: str, name: str, words: str, minutes: int | None, rel: argparse.Namespace) -> int:
    digest, why = sealed_state(run)
    if digest is None:
        print(f"APPROVE=refused: {why}")
        return 1
    if not words.strip():
        die("--words: the owner's words, verbatim")
    manifest = load_manifest(run)
    try:
        meta = gh_repo(name)
    except GhError as exc:
        print(f"APPROVE=refused: {exc}")
        return 1
    problems = [f"the run pushes to {safe(p['url'])}, which is not {meta.get('full_name')}" for p in push_rows(manifest) if norm_url(p["url"]) not in repo_urls(meta)]
    if step == "push" and not push_rows(manifest):
        problems.append("this run pushes nothing (no --git source)")
    preflight: str | None = None
    state = load_state(run)
    if step == "publish":
        pf = _json_file(run / "preflight.json")
        pf_repo = pf.get("repo") if isinstance(pf, dict) and isinstance(pf.get("repo"), dict) else {}
        assert isinstance(pf_repo, dict)
        if not isinstance(pf, dict) or pf.get("problems") or pf_repo.get("id") != meta.get("id") or pf.get("digest") != digest:
            problems.append("no clean preflight for this repository and digest: run preflight first (PREFLIGHT=ok)")
        else:
            preflight = str(pf.get("match"))
    if step == "release" and not release_allowed(state, meta):
        problems.append("a release approval on its own is for a public repository this run pushed to, or one it made public (before a switch, the publish approval covers the release)")
    release = None
    if rel.release_tag:
        release = release_spec(manifest, rel.release_tag, rel.release_target or "", Path(rel.release_title or ""), Path(rel.release_notes or ""), [Path(a) for a in rel.release_asset or []])
    elif step == "release":
        problems.append("--release-tag, --release-target, --release-title and --release-notes are needed")
    if step == "push" and rel.release_tag:
        problems.append("a push approval never covers a release (the release is part of approval 2)")
    path = approval_file(run, step)
    old = _json_file(path)
    if isinstance(old, dict) and approval_closed(old) is None:
        problems.append(f"an open {step} approval exists (until {old.get('expires_at')}): revoke it first")
    if problems:
        for p in problems:
            print(f"APPROVE=refused: {p}")
        return 1
    if path.exists():  # the earlier record stays, under its own time (never over another kept one)
        stamp = str(old.get("recorded_at") if isinstance(old, dict) else "old").replace(":", "")
        for k in range(1, 1000):
            kept = path.with_name(f"{step}-{stamp}{'' if k == 1 else f'-{k}'}.json")
            try:
                os.link(path, kept)
            except FileExistsError:
                continue
            path.unlink()
            break
        else:
            die(f"cannot keep the earlier {step} approval: too many kept records")
    now = _now()
    window = minutes if minutes is not None else VALID_MINUTES[step]
    rec = {"format": 1, "step": step, "repo": {"full_name": meta.get("full_name"), "id": meta.get("id"), "node_id": meta.get("node_id")},
           "digest": digest, "seal": sha256_file(run / "SEALED"), "pushes": push_rows(manifest), "preflight": preflight, "release": release,
           "owner_words": words, "recorded_at": now.isoformat(timespec="seconds"),
           "expires_at": (now + timedelta(minutes=window)).isoformat(timespec="seconds"), "revoked": None, "used": None}  # fmt: skip
    _write_json(path, rec)
    print(f"APPROVED {step} {meta.get('full_name')} (id {meta.get('id')}) digest {digest} until {rec['expires_at']}")
    if release:
        print(f"APPROVED_RELEASE tag {release['tag']} at {release['target']}")
    return 0


def cmd_revoke(run: Path, step: str, words: str) -> int:
    path = approval_file(run, step)
    rec = _json_file(path)
    if not isinstance(rec, dict):
        print(f"REVOKE=none: no {step} approval recorded")
        return 1
    rec["revoked"] = {"at": _now().isoformat(timespec="seconds"), "words": words}
    _write_json(path, rec)
    print(f"REVOKED {step} (recorded {rec.get('recorded_at')})")
    return 0


# ── state: what this run has done on the destination, written around every change ──


def load_state(run: Path) -> dict[str, Any]:
    got = _json_file(run / "state.json")
    return got if isinstance(got, dict) else {}


def save_state(run: Path, **updates: object) -> None:
    state = load_state(run)
    state.update(updates)
    _write_json(run / "state.json", state)


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer], capture_output=True, env=GIT_ENV).returncode == 0


def base_problem(push: dict[str, Any]) -> str | None:
    """A push negotiates with the live destination: if it no longer reaches what the check
    subtracted (a ref deleted or moved back), the push would send more than was read."""
    base = [str(s) for s in push.get("base") or []]
    if not base:
        return None
    repo = Path(str(push["repo"]))
    live = remote_shas(repo, str(push["url"]))
    for b in base:
        if not any(b == h or _is_ancestor(repo, b, h) for h in live):
            return f"the destination no longer reaches {b[:12]} (a ref was deleted or moved back since the check): stage again"
    return None


class RunLock:
    """One push or publish at a time per run: two concurrent runs would both pass the
    approval check before either marks it used (O_EXCL lock file, removed on exit)."""

    def __init__(self, run: Path) -> None:
        self.path = run / "approvals" / ".lock"

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            die(f"{self.path} exists: another push or publish of this run is running (remove the file only if none is)", 1)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {_now().isoformat(timespec='seconds')}\n")
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def cmd_push(run: Path, sources: list[Source]) -> int:
    with RunLock(run):
        return _push(run, sources)


def _push(run: Path, sources: list[Source]) -> int:
    ok, manifest = _same(run, sources)
    if not ok or manifest is None:
        return 1
    digest = str(load_manifest(run).get("digest"))
    rec = _json_file(approval_file(run, "push"))
    name = rec.get("repo", {}).get("full_name") if isinstance(rec, dict) and isinstance(rec.get("repo"), dict) else None
    if not name:
        print("PUSH=refused: no push approval — report the seal, wait for the owner, then approve --step push with their words")
        return 1
    try:
        meta = gh_repo(str(name))
    except GhError as exc:
        print(f"PUSH=refused: {exc}")
        return 1
    problems = approval_problems(run, "push", digest, meta)
    problems += [f"the run pushes to {safe(p['url'])}, which is not {meta.get('full_name')}" for p in push_rows(manifest) if norm_url(p["url"]) not in repo_urls(meta)]
    problems += [b for b in (base_problem(p) for p in manifest["pushes"]) if b]  # type: ignore[union-attr]
    problems = problems or approval_problems(run, "push", digest, meta)  # again, last: a revoke during the reads stops it
    if problems:
        for p in problems:
            print(f"PUSH=refused: {p}")
        return 1
    # Written before the push: a run that stops after it pushes again (git sends nothing new)
    # with the still-unused approval, and then records it.
    save_state(run, pushing={"at": _now().isoformat(timespec="seconds"), "repo_id": meta.get("id"), "pushes": push_rows(manifest)})
    for push in manifest["pushes"]:  # type: ignore[union-attr]
        # Only the checked ref: no tags followed, no submodules pushed, no signing push
        # certificate; the destination is the URL the set was computed against.
        proc = subprocess.run(
            ["git", "-C", push["repo"], "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
             "-c", "push.gpgSign=false", "-c", "core.hooksPath=/dev/null", "push", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
             push["url"], f"{push['tip']}:{push['ref']}"],
            capture_output=True, text=True, check=False, env=GIT_ENV,
        )  # fmt: skip
        if proc.returncode != 0:
            print(f"PUSH=failed {safe(push['url'])} {push['ref']}: {safe(proc.stderr.strip()[-300:])}")
            return 1
        print(f"PUSH=ok {safe(push['url'])} {push['ref']} = {push['tip']}")
    mark_used(run, "push", "pushed")
    save_state(run, pushed={"at": _now().isoformat(timespec="seconds"), "repo_id": meta.get("id"), "pushes": push_rows(manifest)})
    return 0


# ── preflight: everything a switch to public would expose, read and compared ────


def preflight_check(run: Path, name: str, *, after_switch: bool = False) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """(inventory, problems, repository) for switching ``name`` to public after this run.
    ``after_switch`` reads the same inventory once the repository is public (to compare)."""
    manifest = load_manifest(run)
    problems: list[str] = []
    try:
        meta = gh_repo(name)
    except GhError as exc:
        return {}, [str(exc)], {}
    inv: dict[str, Any] = {"repo": {k: meta.get(k) for k in ("id", "node_id", "full_name", "created_at", "private", "visibility", "fork", "archived", "is_template")}}
    if not after_switch and meta.get("private") is not True:
        problems.append(f"{meta.get('full_name')} is not private (visibility {meta.get('visibility')})")
    if meta.get("fork") or meta.get("archived") or meta.get("is_template"):
        problems.append("a fork, a template or an archived repository is out of scope")
    perms = meta.get("permissions")
    if not (isinstance(perms, dict) and perms.get("admin") is True):
        problems.append("this account is not an admin of the repository: it cannot switch the visibility (and may not see everything)")
    pushes = manifest.get("pushes") or []
    assert isinstance(pushes, list)
    if not pushes:
        problems.append("this run pushes nothing: a switch is only for a repository that received this run's checked push")
    # Approval 1 was given and used for this repository, and this run recorded the push.
    approved = _json_file(approval_file(run, "push"))
    state = load_state(run)
    if not (isinstance(approved, dict) and isinstance(approved.get("repo"), dict) and approved["repo"].get("id") == meta.get("id")
            and isinstance(approved.get("used"), dict) and approved["used"].get("result") == "pushed"):  # fmt: skip
        problems.append("approval 1 was not given and used for this repository: approve --step push, then push")
    if not (isinstance(state.get("pushed"), dict) and state["pushed"].get("repo_id") == meta.get("id") and state["pushed"].get("pushes") == push_rows(manifest)):
        problems.append("this run has not recorded its push to this repository")
    for p in pushes:
        if norm_url(str(p["url"])) not in repo_urls(meta):
            problems.append(f"the run pushed to {safe(str(p['url']))}, not to {meta.get('full_name')}")
        if not p.get("whole_history"):
            problems.append(f"{p['ref']}: staged without --whole-history, so this run did not read the history the destination already held")
    want = {str(p["ref"]): str(p["tip"]) for p in pushes}
    # Every ref change GitHub recorded: a force push or a deleted branch leaves commits on
    # GitHub that anyone can fetch by id once the repository is public, so only plain pushes
    # and branch creations of commits inside this run's checked history are accepted.
    try:
        activity = [a for a in gh_list(f"repos/{name}/activity") if isinstance(a, dict)]
        inv["activity"] = len(activity)
        tips = {str(p["ref"]): (Path(str(p["repo"])), str(p.get("commit") or p["tip"])) for p in pushes}
        for a in activity:
            kind, ref, after = str(a.get("activity_type")), str(a.get("ref")), str(a.get("after") or "")
            if kind not in ("push", "branch_creation"):
                problems.append(f"activity: a {kind} on {ref} — commits it removed stay fetchable by id once public")
            elif ref not in tips:
                problems.append(f"activity: a {kind} on {ref}, which is not a checked push of this run")
            elif not (after == tips[ref][1] or _is_ancestor(tips[ref][0], after, tips[ref][1])):
                problems.append(f"activity: {ref} once held {after[:12]}, outside this run's checked history")
    except GhError as exc:
        problems.append(f"could not read activity: {exc}")
    try:  # the activity log covers 90 days: an older repository's ref history cannot all be read
        age = _now() - datetime.fromisoformat(str(meta.get("created_at")).replace("Z", "+00:00"))
        if age.days >= ACTIVITY_DAYS:
            problems.append(f"the repository was created {age.days} days ago: GitHub's activity log covers {ACTIVITY_DAYS} days, so its ref history cannot all be read")
    except ValueError:
        problems.append("the repository's creation time is unreadable")
    for p in pushes:  # statuses and check runs on the checked commits become public too
        commit = str(p.get("commit") or p["tip"])
        try:
            statuses = len(gh_list(f"repos/{name}/commits/{commit}/statuses"))
            checks = gh_total(f"repos/{name}/commits/{commit}/check-runs")
        except GhError as exc:
            problems.append(f"could not read the statuses or check runs of {commit[:12]}: {exc}")
            continue
        inv[f"statuses_{commit[:12]}"] = [statuses, checks]
        if statuses or checks:
            problems.append(f"{commit[:12]} carries {statuses} status(es) and {checks} check run(s) that were never checked")
    lists = {"issues_and_pull_requests": "issues?state=all", "pull_requests": "pulls?state=all", "commit_comments": "comments",
             "releases": "releases", "milestones": "milestones?state=all", "deployments": "deployments", "forks": "forks"}  # fmt: skip
    for label, path in lists.items():
        if after_switch and label == "releases":
            continue  # the approved release may follow the switch; it is compared on its own
        try:
            inv[label] = len(gh_list(f"repos/{name}/{path}"))
        except GhError as exc:
            inv[label] = None
            problems.append(f"could not read {label}: {exc}")
            continue
        if inv[label]:
            problems.append(f"{label}: {inv[label]} — a repository switched by this skill holds none (they were never checked)")
    try:  # tags other than a checked push of this run
        tags = [x for x in gh_list(f"repos/{name}/tags") if isinstance(x, dict)]
        inv["tags"] = sorted(str(x.get("name")) for x in tags if want.get(f"refs/tags/{x.get('name')}") is None)
        if inv["tags"] and not after_switch:
            problems.append(f"tags: {', '.join(inv['tags'])[:200]} — not checked pushes of this run")
    except GhError as exc:
        problems.append(f"could not read tags: {exc}")
    for label, path in {"actions_runs": "actions/runs", "actions_artifacts": "actions/artifacts", "environments": "environments"}.items():
        try:
            inv[label] = gh_total(f"repos/{name}/{path}")
        except GhError as exc:
            inv[label] = None
            problems.append(f"could not read {label}: {exc}")
            continue
        if inv[label]:
            problems.append(f"{label}: {inv[label]} — Actions history, logs and artefacts become public too")
    try:
        labels = sorted(str(x.get("name")) for x in gh_list(f"repos/{name}/labels") if isinstance(x, dict))
        inv["labels"] = labels
        extra = [x for x in labels if x not in DEFAULT_LABELS]
        if extra:
            problems.append(f"labels beyond GitHub's defaults were never checked: {', '.join(extra)[:200]}")
    except GhError as exc:
        problems.append(f"could not read labels: {exc}")
    try:
        inv["rulesets"] = sorted(f"{x.get('name')} ({x.get('target')}, {x.get('enforcement')})" for x in gh_list(f"repos/{name}/rulesets") if isinstance(x, dict))
    except GhError as exc:
        problems.append(f"could not read rulesets: {exc}")
    try:
        inv["pages_site"] = gh_api(f"repos/{name}/pages", missing_ok=True) is not None
        if inv["pages_site"] or meta.get("has_pages"):
            problems.append("a GitHub Pages site exists: its content was never checked")
    except GhError as exc:
        problems.append(f"could not read the Pages site: {exc}")
    inv["features"] = {k: meta.get(k) for k in ("has_issues", "has_wiki", "has_discussions", "has_pages", "has_projects")}
    if meta.get("has_wiki"):
        problems.append("the wiki is on: its pages are never checked — turn it off first")
    if meta.get("has_discussions"):
        problems.append("discussions are on: never checked — turn them off first")
    if pushes:  # a wiki switched off keeps its pages; switched on again they would show
        wiki = re.sub(r"(\.git)?/*$", "", str(pushes[0]["url"])) + ".wiki.git"
        proc = subprocess.run(["git", "ls-remote", wiki], capture_output=True, text=True, check=False, env={**GIT_ENV, "GIT_TERMINAL_PROMPT": "0"})
        local = "://" not in wiki and not re.match(r"^[\w.-]+@", wiki)
        if proc.returncode == 0:
            inv["wiki_refs"] = len([ln for ln in proc.stdout.splitlines() if "\t" in ln])
        elif re.search(r"Repository not found|repository '[^']*' not found", proc.stderr) or (local and "does not appear to be a git repository" in proc.stderr):
            inv["wiki_refs"] = 0  # no wiki repository at all
        else:
            inv["wiki_refs"] = None
            problems.append(f"could not read the wiki repository: {safe(proc.stderr.strip()[-200:])}")
        if inv["wiki_refs"]:
            problems.append(f"the wiki repository holds {inv['wiki_refs']} ref(s) of pages nobody checked")
    staged = [e for e in manifest["entries"] if str(e.get("origin", "")).lower() == f"repo:meta:{name}".lower()]  # type: ignore[union-attr]
    live = public_meta(meta)
    inv["metadata"] = live
    if not staged:
        problems.append("the description, homepage and topics were not part of the checked set: add --repo-meta OWNER/NAME to the run")
    elif _json_file(run / "tree" / str(staged[0]["path"])) != live:
        problems.append("the description, homepage or topics changed since the check")
    if pushes:  # read last: every ref the destination has, GitHub's own included, against the checked pushes
        proc = subprocess.run(["git", "ls-remote", str(pushes[0]["url"])], capture_output=True, text=True, check=False, env={**GIT_ENV, "GIT_TERMINAL_PROMPT": "0"})
        if proc.returncode != 0:
            problems.append(f"git ls-remote failed: {safe(proc.stderr.strip()[-200:])}")
        else:
            refs = {ln.split("\t", 1)[1]: ln.split("\t", 1)[0] for ln in proc.stdout.splitlines() if "\t" in ln}
            head = refs.pop("HEAD", None)
            for ref, sha in sorted(refs.items()):
                if ref.endswith("^{}") and ref[:-3] in want:
                    continue
                if after_switch and ref.startswith("refs/tags/") and ref[len("refs/tags/"):] == load_state(run).get("releasing", {}).get("tag"):
                    continue  # the approved release's own tag
                if want.get(ref) != sha:
                    problems.append(f"ref {ref} = {sha[:12]} is not a checked push of this run")
            for ref, tip in want.items():
                if refs.get(ref) != tip:
                    problems.append(f"{ref} on the destination is {(refs.get(ref) or 'absent')[:12]}, not the checked {tip[:12]}: push first")
            if head is not None and head not in want.values():
                problems.append("the destination's HEAD points outside the checked pushes")
            inv["refs"] = sorted(([ref, sha] for ref, sha in refs.items()), key=lambda r: r[0])
    return inv, problems, meta


def inventory_match(inv: dict[str, Any], digest: str) -> str:
    return hashlib.sha256(json.dumps({"digest": digest, "inventory": inv}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inventory_core(inv: dict[str, Any]) -> dict[str, Any]:
    """The inventory without what the switch itself changes (visibility) or may add (the release)."""
    core = {k: v for k, v in inv.items() if k not in ("releases",)}
    core["repo"] = {k: v for k, v in (inv.get("repo") or {}).items() if k not in ("private", "visibility")}
    core["features"] = {k: v for k, v in (inv.get("features") or {}).items()}
    return core


def cmd_preflight(run: Path, name: str) -> int:
    digest, why = sealed_state(run)
    if digest is None:
        print(f"PREFLIGHT=refused: {why}")
        return 1
    inv, problems, _ = preflight_check(run, name)
    match = inventory_match(inv, digest)
    _write_json(run / "preflight.json", {"format": 1, "digest": digest, "repo": inv.get("repo", {}), "checked_at": _now().isoformat(timespec="seconds"),
                                         "inventory": inv, "match": match, "problems": problems})  # fmt: skip
    for key in ("refs", "metadata", "issues_and_pull_requests", "commit_comments", "releases", "tags", "milestones", "deployments", "forks",
                "actions_runs", "actions_artifacts", "environments", "labels", "rulesets", "pages_site", "features", "wiki_refs", "activity"):  # fmt: skip
        if key in inv:
            print(f"INVENTORY {key} {safe(json.dumps(inv[key], ensure_ascii=False))[:300]}")
    for p in problems:
        print(f"PREFLIGHT=refused: {p}")
    if not problems:
        print(f"PREFLIGHT=ok {match} {inv['repo'].get('full_name')} (id {inv['repo'].get('id')}, created {inv['repo'].get('created_at')})")
    return 1 if problems else 0


# ── publish: the switch to public, then the approved release, then a look without login ──


def staged_copy(run: Path, rel_path: object, sha: object) -> Path:
    """The run's own read-only copy of a release file, recorded in the approval (what is
    uploaded), checked against the approved hash."""
    path = run / "tree" / str(rel_path)
    if not path.is_file() or sha256_file(path) != sha:
        die(f"the staged copy {rel_path} is missing or not the approved bytes")
    return path


def release_files_problem(spec: dict[str, Any], title: Path | None, notes: Path | None, assets: list[Path]) -> str | None:
    """The files given on the command line, if any, must be the approved ones (the upload
    always uses the run's staged copies)."""
    if title is not None and (not title.is_file() or sha256_file(title) != spec.get("title_sha256")):
        return f"{title}: not the approved release title"
    if notes is not None and (not notes.is_file() or sha256_file(notes) != spec.get("notes_sha256")):
        return f"{notes}: not the approved release notes"
    if assets and sorted((a.name, sha256_file(a)) for a in assets if a.is_file()) != sorted((str(x["name"]), str(x["sha256"])) for x in spec.get("assets") or []):
        return "the assets are not the approved ones"
    return None


def tag_target(name: str, tag: str) -> str | None:
    """The commit an existing tag points at (None: no such tag)."""
    ref = gh_api(f"repos/{name}/git/ref/tags/{tag}", missing_ok=True)
    if ref is None:
        return None
    obj = ref.get("object") if isinstance(ref, dict) else None
    if not isinstance(obj, dict):
        raise GhError(f"tag {tag}: unreadable ref")
    while obj.get("type") == "tag":
        tag_obj = gh_api(f"repos/{name}/git/tags/{obj.get('sha')}")
        obj = tag_obj.get("object") if isinstance(tag_obj, dict) else None
        if not isinstance(obj, dict):
            raise GhError(f"tag {tag}: unreadable tag object")
    return str(obj.get("sha"))


def anonymous_get(path: str) -> tuple[int, object]:
    """A GET with no credentials at all: what any visitor sees."""
    req = urllib.request.Request(f"{API_URL}/{path}", headers={"Accept": "application/vnd.github+json", "User-Agent": "agrodesign-github-publish"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https API (or a test URL)
            return int(resp.status), json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def _norm_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def release_mismatch(found: dict[str, Any], tag: str, title: str, notes: str, assets: list[Path]) -> str | None:
    """Why a release GitHub shows is not exactly the approved one (None: it is)."""
    if found.get("tag_name") != tag or found.get("draft") or found.get("prerelease"):
        return f"tag {found.get('tag_name')}, draft {found.get('draft')}, pre-release {found.get('prerelease')}"
    if _norm_text(str(found.get("name") or "")) != _norm_text(title):
        return "its title is not the approved one"
    if _norm_text(str(found.get("body") or "")) != _norm_text(notes):
        return "its notes are not the approved ones"
    got = sorted((str(a.get("name")), int(a.get("size") or 0)) for a in found.get("assets") or [] if isinstance(a, dict))
    if got != sorted((a.name, a.stat().st_size) for a in assets):
        return f"its assets {got} are not the approved ones"
    return None


def do_release(run: Path, name: str, spec: dict[str, Any], recheck: Callable[[], list[str]]) -> int:
    state = load_state(run)
    tag, target = str(spec["tag"]), str(spec["target"])
    title_path = staged_copy(run, spec.get("title_path"), spec.get("title_sha256"))
    notes_path = staged_copy(run, spec.get("notes_path"), spec.get("notes_sha256"))
    approved_assets = [(str(a["name"]), staged_copy(run, a.get("path"), a.get("sha256"))) for a in spec.get("assets") or []]
    title, notes = title_path.read_text(encoding="utf-8").strip(), notes_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="agrodesign-github-publish-assets-") as tmp:
        uploads: list[Path] = []  # each staged copy under its approved name
        for k, (asset_name, src) in enumerate(approved_assets):
            d = Path(tmp) / str(k)
            d.mkdir()
            shutil.copyfile(src, d / asset_name)
            uploads.append(d / asset_name)
        try:
            meta = gh_repo(name)
            all_releases = [r for r in gh_list(f"repos/{name}/releases") if isinstance(r, dict)]
            pointed = tag_target(name, tag)
        except GhError as exc:
            print(f"RELEASE=refused: {exc}")
            return 1
        if meta.get("private") is not False:
            print(f"RELEASE=refused: {name} is not public now — a release goes out only on the repository this run made (or found) public")
            return 1
        mine = isinstance(state.get("releasing"), dict) and state["releasing"].get("tag") == tag
        same_tag = [r for r in all_releases if r.get("tag_name") == tag]
        if any(r.get("draft") for r in same_tag):
            print(f"RELEASE=refused: a draft release for {tag} exists (an interrupted asset upload?) — delete it on GitHub, then run publish again")
            return 1
        if same_tag:
            bad = release_mismatch(same_tag[0], tag, title, notes, uploads)
            if mine and not bad and pointed == target:
                save_state(run, released={"at": _now().isoformat(timespec="seconds"), "tag": tag, "target": target, "url": same_tag[0].get("html_url")})
                print(f"RELEASE=ok {tag} (found after an interrupted run, identical to the approved one)")
                return 0
            print(f"RELEASE=refused: a release for {tag} already exists" + (f" and differs: {bad}" if bad else "") + " — this run did not make it as approved")
            return 1
        if pointed is not None and pointed != target:
            print(f"RELEASE=refused: tag {tag} already exists and points at {pointed[:12]}, not the approved {target[:12]}")
            return 1
        last = recheck()  # the approval once more, right before the call: a revoke during the reads stops it
        if last:
            return _refuse("RELEASE", last)
        save_state(run, releasing={"at": _now().isoformat(timespec="seconds"), "tag": tag, "target": target})
        proc = gh("release", "create", tag, "--repo", name, "--target", target, "--title", title, "--notes-file", str(notes_path), "--latest", *[str(u) for u in uploads])
        if proc.returncode != 0:
            print(f"RELEASE=failed: {safe((proc.stderr or proc.stdout).strip()[-300:])} — run publish again to resume (the state is read first)")
            return 1
        try:
            made = gh_api(f"repos/{name}/releases/tags/{tag}")
            pointed = tag_target(name, tag)
        except GhError as exc:
            print(f"RELEASE=unconfirmed: {exc} — run state to see what GitHub holds")
            return 1
        bad = release_mismatch(made, tag, title, notes, uploads) if isinstance(made, dict) else "unreadable"
    if bad or pointed != target or not isinstance(made, dict):
        print(f"RELEASE=wrong: GitHub shows tag {tag} at {str(pointed)[:12]} ({bad}) — check it by hand")
        return 1
    save_state(run, released={"at": _now().isoformat(timespec="seconds"), "tag": tag, "target": target, "url": made.get("html_url")})
    print(f"RELEASE=ok {tag} at {target} {made.get('html_url')}")
    return 0


def cmd_publish(run: Path, name: str, title: Path | None, notes: Path | None, assets: list[Path]) -> int:
    with RunLock(run):
        return _publish(run, name, title, notes, assets)


def _refuse(prefix: str, problems: list[str]) -> int:
    for p in problems:
        print(f"{prefix}=refused: {p}")
    return 1


def after_switch(run: Path, name: str, digest: str, before: dict[str, Any]) -> tuple[str, list[str]]:
    """Read the inventory again once the repository is public. ("ok", []) when it equals
    ``before`` (the approved, pre-switch inventory) apart from the visibility; ("private", [])
    when GitHub still shows it private after a few reads (the switch is not confirmed);
    ("unreadable", reasons) when reads kept failing; ("changed", reasons) otherwise."""
    for attempt in range(4):
        inv2, post, after = preflight_check(run, name, after_switch=True)
        unreadable = bool(post) and all(p.startswith(("could not read", "git ls-remote failed", "gh api ")) for p in post)
        if (after.get("private") is False and not unreadable) or (post and not unreadable):
            break
        if attempt < 3:  # GitHub can show the old visibility, or fail a read, for a moment
            time.sleep(RETRY_SECONDS * (attempt + 1))
    if post:
        return ("unreadable" if unreadable else "changed"), post
    if after.get("private") is not False:
        return "private", []
    if inventory_match(inventory_core(inv2), digest) != inventory_match(inventory_core(before), digest):
        return "changed", ["the inventory changed during the switch"]
    return "ok", []


def revert(run: Path, name: str, repo_id: object, reasons: list[str], kind: str = "changed") -> int:
    back = gh("repo", "edit", name, "--visibility", "private", "--accept-visibility-change-consequences")
    save_state(run, reverted={"at": _now().isoformat(timespec="seconds"), "repo_id": repo_id, "ok": back.returncode == 0, "kind": kind, "why": reasons[:5]})
    mark_used(run, "publish", "reverted")
    what = "the repository changed around the switch" if kind == "changed" else "GitHub could not be read right after the switch (nothing is known to have changed)"
    print(f"PUBLISH=reverted: {what}, so it was switched back to private" + ("" if back.returncode == 0 else " — the switch back FAILED: make it private by hand now"))
    for p in reasons:
        print(f"PUBLISH=reverted: {p}")
    return 1


def _publish(run: Path, name: str, title: Path | None, notes: Path | None, assets: list[Path]) -> int:
    digest, why = sealed_state(run)
    if digest is None:
        print(f"PUBLISH=refused: {why}")
        return 1
    try:
        meta = gh_repo(name)
    except GhError as exc:
        print(f"PUBLISH=refused: {exc}")
        return 1
    state = load_state(run)
    pub = _json_file(approval_file(run, "publish"))
    reverted = state.get("reverted")
    if isinstance(reverted, dict):
        fresh = (reverted.get("kind") == "read failure" and reverted.get("ok") and meta.get("private") is True and isinstance(pub, dict)
                 and approval_closed(pub) is None and str(pub.get("recorded_at")) > str(reverted.get("at")))  # fmt: skip
        if not fresh:
            if reverted.get("kind") == "read failure" and reverted.get("ok"):
                print("PUBLISH=refused: a read failed right after this run's switch and it was switched back; run preflight, ask the owner again, record a new publish approval, then publish")
            else:
                print("PUBLISH=refused: this run found a change around its switch and " + ("switched the repository back to private" if reverted.get("ok") else "could NOT switch it back — make it private by hand now")
                      + "; stage a new run and ask the owner again")  # fmt: skip
            return 1
        save_state(run, reverted_earlier=reverted, reverted=None, publishing=None)  # a fresh approval after a read failure
        state = load_state(run)
    rel = _json_file(approval_file(run, "release"))
    pub_spec = pub.get("release") if isinstance(pub, dict) and isinstance(pub.get("release"), dict) else None
    switched_here = False
    if not isinstance(state.get("published"), dict):
        if meta.get("private") is False:
            if isinstance(state.get("publishing"), dict) and state["publishing"].get("repo_id") == meta.get("id"):
                # The switch happened in a run that stopped before recording it: check it now,
                # against the inventory the owner approved, before calling it published.
                pf = _json_file(run / "preflight.json")
                if not (isinstance(pf, dict) and isinstance(pub, dict) and pf.get("match") == pub.get("preflight") and isinstance(pf.get("inventory"), dict)):
                    return revert(run, name, meta.get("id"), ["the approved preflight is no longer on record"])
                status, reasons = after_switch(run, name, digest, pf["inventory"])
                if status in ("changed", "unreadable"):
                    return revert(run, name, meta.get("id"), reasons, "changed" if status == "changed" else "read failure")
                if status == "private":
                    print("PUBLISH=unconfirmed: the repository reads private again — nothing recorded; run state, then publish again")
                    return 1
                save_state(run, published={"at": _now().isoformat(timespec="seconds"), "repo_id": meta.get("id"), "note": "found public after an interrupted run, checked"})
                mark_used(run, "publish", "published")
                switched_here = True
                print(f"PUBLISH=ok {meta.get('html_url') or name} (the switch of an interrupted run, checked now)")
            elif not (release_allowed(state, meta) and isinstance(rel, dict) and approval_closed(rel) is None):
                print(f"PUBLISH=refused: {name} is already public, and not by this run (a release there needs this run's push and an open approve --step release)")
                return 1
            # else: a public repository this run pushed to — no switch, only the approved release
        else:
            problems = approval_problems(run, "publish", digest, meta)
            if pub_spec:
                bad = release_files_problem(pub_spec, title, notes, assets)
                if bad:
                    problems.append(bad)
            inv, pre, _ = preflight_check(run, name)  # once more, immediately before the switch
            problems += pre
            if not pre and isinstance(pub, dict) and inventory_match(inv, digest) != pub.get("preflight"):
                problems.append("the destination changed since the approved preflight: run preflight and ask the owner again")
            problems = problems or approval_problems(run, "publish", digest, meta)  # again, last: a revoke during the reads stops it
            if problems:
                return _refuse("PUBLISH", problems)
            save_state(run, publishing={"at": _now().isoformat(timespec="seconds"), "repo_id": meta.get("id")})
            proc = gh("repo", "edit", name, "--visibility", "public", "--accept-visibility-change-consequences")
            if proc.returncode != 0:
                print(f"PUBLISH=failed: {safe((proc.stderr or proc.stdout).strip()[-300:])} — run publish again to resume (the state is read first)")
                return 1
            # Read everything again now that it is public: anything that arrived between the
            # last read and the switch is caught here, and the repository goes back to private.
            status, reasons = after_switch(run, name, digest, inv)
            if status in ("changed", "unreadable"):
                return revert(run, name, meta.get("id"), reasons, "changed" if status == "changed" else "read failure")
            if status == "private":
                print("PUBLISH=unconfirmed: GitHub still shows the repository private — run state, then publish again")
                return 1
            save_state(run, published={"at": _now().isoformat(timespec="seconds"), "repo_id": meta.get("id")})
            mark_used(run, "publish", "published")
            switched_here = True
            print(f"PUBLISH=ok {meta.get('html_url') or name}")
    else:
        switched_here = state["published"].get("repo_id") == meta.get("id")
    # The release: a release approval, if one was recorded, decides (no falling back to the
    # publish approval once a release approval exists, open or not); otherwise the release
    # approval 2 named, and only on the repository this run itself switched.
    if not isinstance(load_state(run).get("released"), dict):
        if isinstance(rel, dict):
            spec, step = (rel.get("release") if isinstance(rel.get("release"), dict) else None), "release"
        elif switched_here and pub_spec:
            spec, step = pub_spec, "publish"
        else:
            spec, step = None, ""
        if spec is not None:
            used_ok = "published" if step == "publish" else None
            problems = approval_problems(run, step, digest, meta, used_ok=used_ok)
            bad = release_files_problem(spec, title, notes, assets)
            if bad:
                problems.append(bad)
            if problems:
                return _refuse("RELEASE", problems)
            if do_release(run, name, spec, lambda: approval_problems(run, step, digest, meta, used_ok=used_ok)) != 0:
                return 1
            mark_used(run, step, "published+released" if step == "publish" else "released")
    return public_check(run, name)


def public_check(run: Path, name: str) -> int:
    """Read the repository (and the release) the way a visitor does: no credentials."""
    released = load_state(run).get("released")
    paths = [f"repos/{name}"] + ([f"repos/{name}/releases/tags/{released['tag']}"] if isinstance(released, dict) else [])
    for path in paths:
        for attempt in range(4):
            status, body = anonymous_get(path)
            if status == 200:
                break
            time.sleep(5 * (attempt + 1))
        if status != 200:
            print(f"PUBLIC=not-visible {path}: HTTP {status} — GitHub can take a minute; run state again")
            return 1
        if path == f"repos/{name}" and isinstance(body, dict) and body.get("private") is not False:
            print(f"PUBLIC=not-visible {path}: still private")
            return 1
    print(f"PUBLIC=ok {', '.join(paths)} without login")
    return 0


def cmd_state(run: Path, name: str) -> int:
    state = load_state(run)
    for key in ("pushing", "pushed", "publishing", "published", "reverted", "releasing", "released"):
        if key in state:
            print(f"STATE {key} {json.dumps(state[key], ensure_ascii=False)}")
    for step in APPROVAL_STEPS:
        rec = _json_file(approval_file(run, step))
        if isinstance(rec, dict):
            print(f"APPROVAL {step} recorded {rec.get('recorded_at')} — {approval_closed(rec) or 'open until ' + str(rec.get('expires_at'))}")
    try:
        meta = gh_repo(name)
        print(f"LIVE {meta.get('full_name')} visibility {meta.get('visibility')}")
        released = state.get("released") or state.get("releasing")
        if isinstance(released, dict):
            print(f"LIVE tag {released.get('tag')} at {tag_target(name, str(released.get('tag')))}")
    except GhError as exc:
        print(f"LIVE unreadable: {exc}")
        return 1
    return 0


def _abs_path(raw: str) -> Path:
    """An argument path made absolute (gitleaks runs in a temporary cwd)."""
    return Path(os.path.abspath(raw))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    p = argparse.ArgumentParser(prog=f"github_upload_check.py {cmd}")
    if cmd == "stage":
        p.add_argument("--out", required=True, type=_abs_path)
        known, extra = p.parse_known_args(rest)
        return cmd_stage(known.out, parse_sources(extra))
    if cmd in ("verify", "push"):
        p.add_argument("--run", required=True, type=_abs_path)
        known, extra = p.parse_known_args(rest)
        return (cmd_verify if cmd == "verify" else cmd_push)(known.run, parse_sources(extra))
    if cmd == "scan":
        p.add_argument("--run", required=True, type=_abs_path)
        p.add_argument("--values-file", type=Path)
        a = p.parse_args(rest)
        return cmd_scan(a.run, a.values_file)
    if cmd == "bundle":
        p.add_argument("--run", required=True, type=_abs_path)
        p.add_argument("--max-bytes", type=int, default=BUNDLE_MAX_BYTES)
        a = p.parse_args(rest)
        if a.max_bytes < 10_000:
            die("--max-bytes below 10000")
        return cmd_bundle(a.run, a.max_bytes)
    if cmd == "seal":
        p.add_argument("--run", required=True, type=_abs_path)
        return cmd_seal(p.parse_args(rest).run)
    if cmd == "gate":
        p.add_argument("--run", required=True, type=_abs_path)
        p.add_argument("--before", required=True, choices=("codex", "grok"))
        a = p.parse_args(rest)
        return cmd_gate(a.run, a.before)
    if cmd in ("approve", "revoke", "preflight", "publish", "state"):
        p.add_argument("--run", required=True, type=_abs_path)
        if cmd in ("approve", "revoke"):
            p.add_argument("--step", required=True, choices=APPROVAL_STEPS)
            p.add_argument("--words", required=True, help="the owner's words, verbatim")
        if cmd != "revoke":
            p.add_argument("--repo", required=True, help="OWNER/NAME on GitHub")
        if cmd == "approve":
            p.add_argument("--valid-minutes", type=int)
            p.add_argument("--release-tag")
            p.add_argument("--release-target")
        if cmd in ("approve", "publish"):
            p.add_argument("--release-title", type=_abs_path)
            p.add_argument("--release-notes", type=_abs_path)
            p.add_argument("--release-asset", type=_abs_path, action="append", default=[])
        a = p.parse_args(rest)
        if cmd != "revoke" and not REPO_NAME.fullmatch(a.repo):
            die(f"--repo needs OWNER/NAME, got {a.repo!r}")
        if cmd == "approve":
            if a.valid_minutes is not None and not 1 <= a.valid_minutes <= 7 * 24 * 60:
                die("--valid-minutes: between 1 and 10080")
            return cmd_approve(a.run, a.step, a.repo, a.words, a.valid_minutes, a)
        if cmd == "revoke":
            return cmd_revoke(a.run, a.step, a.words)
        if cmd == "preflight":
            return cmd_preflight(a.run, a.repo)
        if cmd == "publish":
            return cmd_publish(a.run, a.repo, a.release_title, a.release_notes, a.release_asset)
        return cmd_state(a.run, a.repo)
    die(f"unknown command {cmd!r}: stage, scan, bundle, gate, seal, verify, approve, revoke, push, preflight, publish or state")
    return 2


if __name__ == "__main__":
    sys.exit(main())
