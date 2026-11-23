"""Build, and afterwards verify, the permission profile that keeps a Codex run's commands away from secrets.

usage: python3 -I codex_read_deny.py table <codex-command> [--root DIR]... [--deny PATH]...
       python3 -I codex_read_deny.py check <events.jsonl> <sessions-dir> <table>

Why. `codex exec --sandbox read-only` lets the commands Codex runs read every file the
OS account can read, and whatever they read goes to OpenAI as tool output: the
account's own credentials under its home, a project's `.env`, a key file elsewhere on
the machine. Codex's permission profiles (codex-cli 0.156) can deny reads per path; a
denied path stays unreadable by its absolute path, through a link to the file or to its
directory and from a script's open() (measured with decoy files and real runs).

`table` prints the TOML inline table that goes into
`-c permissions.<name>.filesystem=<table>`. It denies:
- every entry of the account's home except the chain down to the installed Codex
  package. The whole home cannot be denied: the sandbox runs the Codex binary from
  there, and a `read` entry inside a denied directory does not reopen it (measured), so
  the siblings at each level of that chain are denied one by one. Links are skipped:
  reading through one reaches its target, which is governed where it is.
- every other home under /home (Linux) or /Users (macOS) this account can traverse.
- CODEX_HOME when it is set (it holds auth.json).
- for each --root DIR, the target of every name at its top that begins with `.env`
  (`.env`, `.env.*`, `.envrc`, an `.envs` folder; not `*.example`), so a link and the
  file it points to are both covered. A --root that is not a readable folder fails the
  build.
- each --deny PATH as given (made absolute): your own secret folders outside the home.
A name that cannot be written in TOML (not valid UTF-8) fails the build: an entry the
profile cannot name is one it cannot deny. Exit 1, nothing on stdout, a reason on stderr.

`check` reads the run's thread id from the --json event stream, opens that thread's
rollout log and confirms that every turn_context records each path of <table> as
denied. A loaded config that sets `sandbox_mode` makes Codex ignore the profile, and
the run then had the read-everything sandbox, so the caller treats a failed check as a
run that may have read a secret. Exit 0 when confirmed; exit 1 with one line on stdout
saying what was missing.
"""

import glob
import json
import os
import sys
import tomllib


def toml_key(path: str) -> str:
    """A TOML basic string for path; ValueError when the name is not valid UTF-8."""
    path.encode("utf-8")  # a surrogate-escaped byte raises UnicodeEncodeError (a ValueError)
    out = []
    for ch in path:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def deny_paths(codex: str, roots: list[str], extra: list[str]) -> list[str]:
    home = os.path.realpath(os.environ["HOME"])
    if not os.path.isdir(home):
        raise ValueError(f"HOME is not a directory: {home}")
    deny: list[str] = []

    def add(p: str) -> None:
        if p not in deny:
            deny.append(p)

    real = os.path.realpath(codex)
    # The npm launcher (bin/codex.js) runs a native binary from inside its own package;
    # a native install is the binary itself.
    keep = os.path.dirname(os.path.dirname(real)) if real.endswith(".js") else real
    if keep == home:
        raise ValueError("the Codex install is the home directory itself")
    if keep.startswith(home + os.sep):
        cur = home
        for step in os.path.relpath(keep, home).split(os.sep):
            for name in sorted(os.listdir(cur)):
                p = os.path.join(cur, name)
                if name != step and not os.path.islink(p):
                    add(p)
            cur = os.path.join(cur, step)
    else:
        add(home)
    homes = os.path.dirname(home)
    if homes in ("/home", "/Users"):
        for name in sorted(os.listdir(homes)):
            p = os.path.join(homes, name)
            if p != home and not os.path.islink(p) and os.path.isdir(p) and os.access(p, os.X_OK):
                add(p)
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        add(os.path.realpath(codex_home))
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError as exc:  # a mistyped root would leave its .env readable unnoticed
            raise ValueError(f"--root {root!r} is not a readable folder ({exc.strerror})") from None
        for name in names:
            if name.startswith(".env") and not name.endswith(".example"):
                target = os.path.realpath(os.path.join(root, name))
                if os.path.exists(target):
                    add(target)
    for p in extra:
        if not os.path.isabs(p):
            raise ValueError(f"--deny {p!r}: give an absolute path")
        add(os.path.realpath(p))
    return deny


def table(argv: list[str]) -> int:
    if len(argv) < 1:
        print("usage: codex_read_deny.py table <codex-command> [--root DIR]... [--deny PATH]...", file=sys.stderr)
        return 2
    codex, rest, roots, extra = argv[0], argv[1:], [], []
    while rest:
        if len(rest) < 2 or rest[0] not in ("--root", "--deny"):
            print(f"codex_read_deny: unexpected argument {rest[0]!r}", file=sys.stderr)
            return 2
        (roots if rest[0] == "--root" else extra).append(rest[1])
        rest = rest[2:]
    try:
        paths = deny_paths(codex, roots, extra)
        body = ", ".join(f'{toml_key(p)} = "deny"' for p in paths)
    except (KeyError, OSError, ValueError) as e:
        print(f"codex_read_deny: cannot build the deny table: {e!r}", file=sys.stderr)
        return 1
    print("{" + body + "}")
    return 0


def recorded_denies(events: str, sessions: str) -> tuple[list[set[str]], str]:
    """The denied paths each turn_context of the run records, or a reason why none could be read."""
    thread = None
    with open(events, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("type") == "thread.started":
                thread = str(e.get("thread_id") or "")
    if not thread or not all(c.isalnum() or c == "-" for c in thread):
        return [], "no thread id in the event stream"
    hits = sorted(glob.glob(os.path.join(sessions, "**", f"rollout-*{thread}*.jsonl"), recursive=True), key=os.path.getmtime, reverse=True)
    if not hits:
        return [], "no rollout log for the run's thread"
    turns: list[set[str]] = []
    with open(hits[0], encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") != "turn_context":
                continue
            p = o.get("payload")
            prof = p.get("permission_profile") if isinstance(p, dict) else None
            fs = prof.get("file_system") if isinstance(prof, dict) else None
            entries = fs.get("entries") if isinstance(fs, dict) else None
            got: set[str] = set()
            for ent in entries if isinstance(entries, list) else []:
                if not isinstance(ent, dict) or ent.get("access") != "deny":
                    continue
                path = ent.get("path")
                if isinstance(path, dict) and path.get("type") == "path" and isinstance(path.get("path"), str):
                    got.add(path["path"])
            turns.append(got)
    if not turns:
        return [], "no turn_context in the rollout log"
    return turns, ""


def check(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: codex_read_deny.py check <events.jsonl> <sessions-dir> <table>", file=sys.stderr)
        return 2
    events, sessions, tab = argv
    try:
        want = set(tomllib.loads("t = " + tab)["t"])
        turns, why = recorded_denies(events, sessions)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"the check could not run: {type(e).__name__}")
        return 1
    if why:
        print(why)
        return 1
    for got in turns:
        missing = want - got
        if missing:
            print(f"{len(missing)} of {len(want)} denied paths are not in the run's permission profile")
            return 1
    print(f"ok denied={len(want)} turns={len(turns)}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "table":
        return table(argv[1:])
    if argv and argv[0] == "check":
        return check(argv[1:])
    print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
