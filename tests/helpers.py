"""Shared fixtures for the portability tests: fictitious values only.

Every secret-shaped string is built at runtime, so no scanner reads a literal one in
this repository; every host name, account and path is made up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
UPLOAD = ROOT / "skills" / "agrodesign-github-publish" / "scripts"
CHECK = UPLOAD / "github_upload_check.py"
GROK = UPLOAD / "grok_review.py"
CODEX = UPLOAD / "codex_review.sh"
READ_DENY = UPLOAD / "lib" / "codex_read_deny.py"
REVIEW = ROOT / "skills" / "agrodesign-deep-review"

#: Fictitious private values (a made-up host and account), built at runtime.
FAKE_HOST = "zeta" + "-intra" + "host.example"
FAKE_ACCOUNT = "svc" + "-quill" + "-runner"
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
FAKE_KEY_LINE = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
STAGES = ("mechanical", "claude", "codex", "grok", "final")

GITLEAKS_STUB = """#!/usr/bin/env bash
prev=""; out=""
for a in "$@"; do [ "$prev" = "--report-path" ] && out="$a"; prev="$a"; done
d="$(cd "$(dirname "$0")" && pwd)"
[ "$1" = "version" ] && { echo "8.0.0-stub"; exit 0; }
printf '[]' > "$out"
"""


#: A stub gh: `gh api PATH`, `gh repo edit … --visibility public`, `gh release create …`,
#: answering from (and writing to) github.json beside it — a made-up GitHub, nothing real.
GH_STUB = r'''#!/usr/bin/env python3
import json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "github.json")
st = json.load(open(state_path))
args = sys.argv[1:]
st.setdefault("log", []).append(args)
def save():
    json.dump(st, open(state_path, "w"), indent=1)
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
def fail(code, text):
    save(); print(json.dumps({"message": text, "status": str(code)})); sys.stderr.write(f"gh: {text} (HTTP {code})\n"); sys.exit(1)
if args[:1] == ["api"]:
    st["api_calls"] = st.get("api_calls", 0) + 1
    if st.get("revoke_file") and st["api_calls"] == st.get("revoke_after_calls"):  # the owner says stop mid-run
        rec = json.load(open(st["revoke_file"]))
        rec["revoked"] = {"at": "2026-01-02T03:04:05+00:00", "words": "stop"}
        json.dump(rec, open(st["revoke_file"], "w"))
    if st["api_calls"] == st.get("private_at_call"):  # someone switches it back mid-run
        for r in st["repos"].values():
            r["private"], r["visibility"] = True, "private"
    path = args[-1]
    bare, _, query = path.partition("?")
    q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
    if any(bare.startswith(f) for f in st.get("fail", [])):
        fail(500, "Server Error")
    if any(bare.startswith(f) for f in st.get("fail_when_public", [])) and any(not r["private"] for r in st["repos"].values()):
        fail(502, "Bad Gateway")
    m = re.match(r"^repos/([^/]+/[^/]+)(?:/(.*))?$", bare)
    if not m or m.group(1).lower() not in {k.lower() for k in st["repos"]}:
        fail(404, "Not Found")
    name = next(k for k in st["repos"] if k.lower() == m.group(1).lower())
    repo, rest = st["repos"][name], m.group(2) or ""
    if rest == "":
        out(repo)
    if rest in ("actions/runs", "actions/artifacts", "environments"):
        out({"total_count": st.get("totals", {}).get(rest, 0)})
    if rest == "pages":
        out({"url": "x"}) if st.get("pages") else fail(404, "Not Found")
    t = re.match(r"^git/ref/tags/(.+)$", rest)
    if t:
        sha = st.get("tag_refs", {}).get(t.group(1))
        out({"ref": "refs/tags/" + t.group(1), "object": {"type": "commit", "sha": sha}}) if sha else fail(404, "Not Found")
    t = re.match(r"^releases/tags/(.+)$", rest)
    if t:
        rel = next((r for r in st.get("lists", {}).get("releases", []) if r.get("tag_name") == t.group(1)), None)
        out(rel) if rel else fail(404, "Not Found")
    if rest in ("issues", "pulls", "comments", "releases", "tags", "milestones", "deployments", "forks", "labels", "rulesets", "activity"):
        items = st.get("lists", {}).get(rest, [])
        page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
        out(items[(page - 1) * per: page * per])
    fail(404, "Not Found")
if args[:2] == ["repo", "edit"]:
    if st.get("edit_fail"):
        save(); sys.stderr.write("edit failed\n"); sys.exit(1)
    name = args[2]
    vis = args[args.index("--visibility") + 1] if "--visibility" in args else None
    if vis == "private" and st.get("edit_private_fail"):
        save(); sys.stderr.write("could not change visibility\n"); sys.exit(1)
    if vis in ("public", "private"):
        st["repos"][name]["private"] = vis == "private"
        st["repos"][name]["visibility"] = vis
        if vis == "public" and st.get("edit_adds_issue"):  # something arrives during the switch
            st.setdefault("lists", {}).setdefault("issues", []).append({"number": 99})
        if vis == "public" and st.pop("edit_fail_after_switch", False):  # switched, but the call reports a failure
            save(); sys.stderr.write("timeout\n"); sys.exit(1)
    out({})
if args[:2] == ["release", "create"]:
    if st.get("release_fail"):
        save(); sys.stderr.write("release failed\n"); sys.exit(1)
    tag = args[2]
    target = args[args.index("--target") + 1]
    st.setdefault("tag_refs", {}).setdefault(tag, target)
    assets = [{"name": os.path.basename(a), "size": os.path.getsize(a)} for a in args[args.index("--latest") + 1:]]
    st.setdefault("uploaded", []).extend(args[args.index("--latest") + 1:])
    st.setdefault("lists", {}).setdefault("releases", []).append({
        "tag_name": tag, "draft": False, "prerelease": False, "html_url": "https://github.example/releases/" + tag,
        "name": args[args.index("--title") + 1], "body": open(args[args.index("--notes-file") + 1]).read(), "assets": assets})
    out({})
save(); sys.stderr.write("unknown gh command\n"); sys.exit(2)
'''


def fake_repo(name: str, clone_url: str, repo_id: int = 4242) -> dict[str, object]:
    """A private, empty, new repository as GitHub's API describes one (made-up values)."""
    return {"id": repo_id, "node_id": f"R_test{repo_id}", "full_name": name, "private": True, "visibility": "private",
            "clone_url": clone_url, "ssh_url": f"git@github.example:{name}.git", "html_url": f"https://github.example/{name}",
            "created_at": "2026-01-02T03:04:05Z", "description": "Made-up skills for tests", "homepage": None, "topics": [],
            "has_issues": False, "has_wiki": False, "has_discussions": False, "has_pages": False, "has_projects": False,
            "fork": False, "archived": False, "is_template": False, "permissions": {"admin": True, "push": True, "pull": True}}  # fmt: skip


class FakeGitHub:
    """The stub gh plus a local, credential-free HTTP API for the anonymous check."""

    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.gh = executable(folder / "gh", GH_STUB)
        self.path = folder / "github.json"
        self.write({"repos": {}, "lists": {"labels": [{"name": "bug"}, {"name": "question"}]}, "totals": {}})
        import http.server
        import threading

        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                if self.headers.get("Authorization"):
                    self.send_response(400)
                    self.end_headers()
                    return
                st = fake.read()
                m = re.match(r"^/repos/([^/]+/[^/]+)(?:/releases/tags/(.+))?$", self.path)
                repo = st["repos"].get(m.group(1)) if m else None
                body: object = None
                if repo and not repo["private"]:
                    if m and m.group(2):
                        body = next((r for r in st.get("lists", {}).get("releases", []) if r.get("tag_name") == m.group(2)), None)
                    else:
                        body = {"full_name": repo["full_name"], "private": False}
                data = json.dumps(body).encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data if body is not None else b"{}")

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())  # type: ignore[no-any-return]

    def write(self, st: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(st, indent=1))

    def update(self, fn: Any) -> None:
        st = self.read()
        fn(st)
        self.write(st)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UploadCase(unittest.TestCase):
    """A temporary folder (outside every home and git work tree) with a stub gitleaks
    and a fictitious values file at $XDG_CONFIG_HOME/agrodesign-github-publish/values.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gu-test-", dir=self._base())
        self.tmp = Path(self._tmp.name)
        self.gitleaks = executable(self.tmp / "gitleaks", GITLEAKS_STUB)
        self.config = self.tmp / "config"
        self.values_path = self.config / "agrodesign-github-publish" / "values.json"
        self.write_values({"host.build": FAKE_HOST, "account.ci": FAKE_ACCOUNT})
        self.env = {**os.environ, "AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN": str(self.gitleaks), "XDG_CONFIG_HOME": str(self.config)}

    def tearDown(self) -> None:
        for p in [self.tmp, *self.tmp.rglob("*")]:
            if not p.is_symlink():
                try:
                    p.chmod(p.stat().st_mode | stat.S_IWUSR)
                except OSError:
                    pass
        self._tmp.cleanup()

    @staticmethod
    def _base() -> str | None:
        """A temporary base outside the home directory (the stage refuses a home)."""
        home = Path.home().resolve()
        for cand in (os.environ.get("TMPDIR"), "/tmp", "/var/tmp"):
            if cand and Path(cand).is_dir() and home not in Path(cand).resolve().parents and Path(cand).resolve() != home:
                return cand
        raise unittest.SkipTest("no temporary folder outside the home directory")

    def write_values(self, values: dict[str, object], fmt: int = 1) -> None:
        self.values_path.parent.mkdir(parents=True, exist_ok=True)
        self.values_path.write_text(json.dumps({"format": fmt, "values": values}))

    def run_check(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(CHECK), *args], capture_output=True, text=True, env=env or self.env, check=False)

    def make_tree(self, files: dict[str, str]) -> Path:
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        for name, body in files.items():
            p = src / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return src

    def staged(self, src: Path, name: str = "run") -> Path:
        run = self.tmp / name
        got = self.run_check("stage", "--out", str(run), "--tree", str(src))
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        return run

    # ── a complete run, every stage's artefact written as the skill writes it ──────
    def ai_paths(self, run: Path) -> list[str]:
        return [e["path"] for e in json.loads((run / "manifest.json").read_text())["entries"] if e["ai"]]

    def digest(self, run: Path) -> str:
        return (run / "DIGEST").read_text().strip()

    def bind(self, run: Path, resolved: dict[str, str]) -> None:
        files = [run / "scan" / "report.json", *run.glob("stage2-claude*.json"), *run.glob("stage3-codex-*.md.codex.md"), *run.glob("stage4-grok-*.json")]
        bound = {f.relative_to(run).as_posix(): sha(f) for f in files if f.is_file()}
        (run / "cleared.json").write_text(json.dumps({"digest": self.digest(run), "bound": bound, "resolved": resolved}))

    def through_claude(self, run: Path, resolved: dict[str, str] | None = None) -> None:
        """scan, bundle, a clean Claude answer, the clearance: the gate before Codex opens."""
        got = self.run_check("scan", "--run", str(run))
        self.assertIn(got.returncode, (0, 1), got.stderr + got.stdout)
        report = json.loads((run / "scan" / "report.json").read_text())
        if report["hits"]:
            (run / "triage.md").write_text("fixture hits: false\n")
        got = self.run_check("bundle", "--run", str(run))
        self.assertEqual(got.returncode, 0, got.stderr)
        (run / "stage2-claude.json").write_text(json.dumps({"digest": self.digest(run), "findings": [], "read": self.ai_paths(run), "not_read": [], "ran_on": "Opus 5.5"}))
        self.resolved = dict(resolved or {})
        self.bind(run, self.resolved)

    def through_codex(self, run: Path) -> None:
        index = json.loads((run / "bundles" / "index.json").read_text())
        for name in index["bundles"]:
            nn = name.removeprefix("bundle-").removesuffix(".md")
            text = (run / "bundles" / name).read_text()
            (run / f"stage3-codex-{nn}.md").write_text("brief\n=== UPLOAD SET ===\n" + text)
            answer = {"findings": [], "read": index["files"][name], "not_read": []}
            (run / f"stage3-codex-{nn}.md.codex.md").write_text("Read all.\n```json\n" + json.dumps(answer) + "\n```\n")
        self.bind(run, self.resolved)

    def through_grok_and_final(self, run: Path) -> None:
        index = json.loads((run / "bundles" / "index.json").read_text())
        for name, bundle_sha in index["bundles"].items():
            nn = name.removeprefix("bundle-").removesuffix(".md")
            (run / f"stage4-grok-{nn}.json").write_text(json.dumps({"bundle_sha256": bundle_sha, "digest": self.digest(run), "findings": [], "not_checked": ""}))
        self.bind(run, self.resolved)
        (run / "stage5-final.json").write_text(json.dumps({"digest": self.digest(run), "verdict": "PUBLISH_OK", "confirmed": [], "refuted": [], "uncovered": [], "ran_on": "Opus 5.5"}))
        stages = {s: {"status": "done"} for s in STAGES}
        (run / "verdict.json").write_text(json.dumps({"digest": self.digest(run), "verdict": "PUBLISH_OK", "stages": stages, "confirmed": [], "dismissed": []}))

    def complete(self, run: Path, resolved: dict[str, str] | None = None) -> None:
        self.through_claude(run, resolved)
        self.through_codex(run)
        self.through_grok_and_final(run)
