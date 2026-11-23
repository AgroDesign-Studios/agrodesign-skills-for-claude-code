"""codex_review.sh and lib/codex_read_deny.py against a stub codex (never the real one):
the order gate, the deny table, the brief scan, the empty working folder, the
served-model and read-deny checks, and no answer file left behind on a refusal."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CHECK, CODEX, FAKE_TOKEN, READ_DENY, UploadCase, executable

STUB = r'''#!/usr/bin/env python3
import json, os, sys, tomllib
here = os.path.dirname(os.path.abspath(__file__))
flag = lambda n: os.path.exists(os.path.join(here, n))
args = sys.argv[1:]
brief = sys.stdin.read()
def opt(name):
    return args[args.index(name) + 1]
table = next(a.split("=", 1)[1] for a in args if a.startswith("permissions.upload-read-deny.filesystem="))
root = opt("-C")
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": args, "root_list": os.listdir(root), "brief_len": len(brief)}) + "\n")
thread = "0000-stub-thread"
print(json.dumps({"type": "thread.started", "thread_id": thread}))
denies = [] if flag("no-profile") else [{"access": "deny", "path": {"type": "path", "path": p}} for p in tomllib.loads("t = " + table)["t"]]
served = "some-other-model" if flag("reroute") else opt("-m")
sessions = os.path.join(os.environ["CODEX_HOME"], "sessions", "2026")
os.makedirs(sessions, exist_ok=True)
with open(os.path.join(sessions, f"rollout-x-{thread}.jsonl"), "w") as fh:
    fh.write(json.dumps({"type": "turn_context", "payload": {"model": served, "permission_profile": {"file_system": {"entries": denies}}}}) + "\n")
if not flag("silent"):
    with open(opt("--output-last-message"), "w") as fh:
        fh.write("No findings.\n```json\n{\"findings\": [], \"read\": [], \"not_read\": []}\n```\n")
'''

#: A gitleaks stand-in for both uses: `dir` (the scan) and `stdin` (the brief scan, which
#: reports a leak for a GitHub-token prefix).
GITLEAKS_BOTH = """#!/usr/bin/env bash
[ "$1" = "version" ] && { echo "8.0.0-stub"; exit 0; }
if [ "$1" = "stdin" ]; then
  if grep -q 'gh''p_'; then exit 1; fi
  exit 0
fi
prev=""; out=""
for a in "$@"; do [ "$prev" = "--report-path" ] && out="$a"; prev="$a"; done
printf '[]' > "$out"
"""


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("timeout"), "the Codex helper needs Linux (prctl) and coreutils timeout")
class CodexReviewCases(UploadCase):
    def setUp(self) -> None:
        super().setUp()
        executable(self.gitleaks, GITLEAKS_BOTH)
        self.home = self.tmp / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / "secrets").mkdir()
        (self.home / ".local" / "share").mkdir()
        self.codex = executable(self.home / ".local" / "bin" / "codex", STUB)
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir()
        self.project = self.make_tree({"README.md": "Public text.\n"})
        self.run_dir = self.staged(self.project)
        self.through_claude(self.run_dir)
        # Written after staging: the folder's .env files are what --root must deny.
        (self.project / ".env").write_text("FIXTURE=1\n")
        (self.project / ".env.example").write_text("X=\n")
        self.brief = self.run_dir / "stage3-codex-01.md"
        self.write_brief()
        path = f"{self.codex.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        self.codex_env = {**self.env, "HOME": str(self.home), "CODEX_HOME": str(self.codex_home), "PATH": path, "CODEX_REVIEW_TIMEOUT": "60"}

    def write_brief(self, extra: str = "") -> None:
        got = subprocess.run([sys.executable, "-I", str(CHECK), "brief", "--run", str(self.run_dir), "--bundle", "01"], capture_output=True, text=True, env=self.env, check=False)
        assert got.returncode == 0, got.stdout + got.stderr
        if extra:  # a brief that is no longer the written one (for the secret scan, which runs first)
            self.brief.write_text(extra + self.brief.read_text())

    def run_codex(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(CODEX), *args], capture_output=True, text=True, env=self.codex_env, check=False)

    def calls(self) -> list[dict[str, object]]:
        f = self.codex.parent / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def answer(self) -> Path:
        return Path(str(self.brief) + ".codex.md")

    def test_ok_run_from_an_empty_folder_under_the_deny_profile(self) -> None:
        extra = self.tmp / "other-secrets"
        extra.mkdir()
        got = self.run_codex("--root", str(self.project), "--deny", str(extra), str(self.brief))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("CODEX_REVIEW=ok served=gpt-6-astra effort=ultra", got.stdout)
        self.assertTrue(self.answer().is_file())
        (call,) = self.calls()
        argv = call["argv"]
        self.assertEqual(call["root_list"], [], "codex's working root is an empty folder")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertNotIn("--sandbox", argv)
        table = next(a for a in argv if a.startswith("permissions.upload-read-deny.filesystem="))
        for denied in (self.home / "secrets", self.home / ".local" / "share", self.codex_home, self.project / ".env", extra):
            self.assertIn(str(denied.resolve()), table)
        self.assertNotIn(str((self.project / ".env.example").resolve()), table)
        self.assertNotIn(f'"{self.codex}"', table, "the Codex install itself stays readable")

    def test_closed_gate_sends_nothing(self) -> None:
        (self.run_dir / "cleared.json").unlink()
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 11, got.stderr)
        self.assertIn("out-of-order", got.stderr)
        self.assertEqual(self.calls(), [])

    def test_brief_without_its_bundle_sends_nothing(self) -> None:
        self.brief.write_text("Review: nothing attached.\n")
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 11)
        self.assertEqual(self.calls(), [])

    def test_brief_that_lost_its_instructions_sends_nothing(self) -> None:
        bundle = (self.run_dir / "bundles" / "bundle-01.md").read_text()
        self.brief.write_text("# Security check\n\nThe text follows the line \"=== UPLOAD SET ===\n" + bundle)
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 11)
        self.assertIn("is not the one", got.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_written_brief_carries_the_instructions_then_the_bundle(self) -> None:
        text = self.brief.read_text()
        bundle = (self.run_dir / "bundles" / "bundle-01.md").read_text()
        for needle in ("Open no file.", "A secrets", "D harmful instructions", "Answer: a short paragraph", "=== END OF UPLOAD SET ==="):
            self.assertIn(needle, text)
        self.assertLess(text.index("Answer: a short paragraph"), text.index(bundle))
        self.assertLess(text.index(bundle), text.index("=== END OF UPLOAD SET ==="))
        self.assertNotIn("<DIGEST>", text)

    def test_brief_that_is_not_a_run_brief_is_refused(self) -> None:
        other = self.tmp / "brief.md"
        other.write_text("hello\n")
        self.assertEqual(self.run_codex(str(other)).returncode, 2)

    def test_secret_in_the_brief_sends_nothing(self) -> None:
        self.write_brief(f"token {FAKE_TOKEN}\n")
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 9)
        self.assertIn("CODEX_REVIEW=secret", got.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.answer().exists())

    def test_missing_gitleaks_sends_nothing(self) -> None:
        self.codex_env["AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN"] = str(self.tmp / "no-such-gitleaks")
        self.codex_env["PATH"] = f"{self.codex.parent}:/usr/bin:/bin"
        got = self.run_codex(str(self.brief))
        if shutil.which("gitleaks", path="/usr/bin:/bin"):
            self.skipTest("a system gitleaks is on the minimal PATH")
        self.assertEqual(got.returncode, 9)
        self.assertIn("gitleaks-missing", got.stderr)

    def test_rerouted_answer_is_deleted(self) -> None:
        (self.codex.parent / "reroute").write_text("")
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 6)
        self.assertIn("served=some-other-model", got.stderr)
        self.assertFalse(self.answer().exists())

    def test_unprotected_answer_is_deleted(self) -> None:
        (self.codex.parent / "no-profile").write_text("")
        self.assertEqual(self.run_codex(str(self.brief)).returncode, 10)
        self.assertFalse(self.answer().exists())

    def test_empty_answer_leaves_no_file(self) -> None:
        (self.codex.parent / "silent").write_text("")
        self.assertEqual(self.run_codex(str(self.brief)).returncode, 5)
        self.assertFalse(self.answer().exists())

    def test_model_override_is_requested_and_checked(self) -> None:
        self.codex_env["CODEX_REVIEW_MODEL"] = "gpt-9-fixture"
        got = self.run_codex(str(self.brief))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("served=gpt-9-fixture", got.stdout)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-9-fixture")

    def test_mistyped_root_is_refused(self) -> None:
        got = self.run_codex("--root", str(self.tmp / "no-such-folder"), str(self.brief))
        self.assertEqual(got.returncode, 3)
        self.assertEqual(self.calls(), [])


class ReadDenyCases(unittest.TestCase):
    def test_relative_deny_path_is_refused(self) -> None:
        got = subprocess.run([sys.executable, "-I", str(READ_DENY), "table", sys.executable, "--deny", "relative/path"], capture_output=True, text=True, check=False)
        self.assertEqual(got.returncode, 1)
        self.assertEqual(got.stdout, "")

    def test_check_finds_a_missing_deny(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n")
            (t / "sessions").mkdir()
            entry = {"access": "deny", "path": {"type": "path", "path": "/a"}}
            (t / "sessions" / "rollout-t-1.jsonl").write_text(json.dumps({"type": "turn_context", "payload": {"permission_profile": {"file_system": {"entries": [entry]}}}}) + "\n")
            ok = subprocess.run([sys.executable, "-I", str(READ_DENY), "check", str(t / "events.jsonl"), str(t / "sessions"), '{"/a" = "deny"}'], capture_output=True, text=True, check=False)
            self.assertEqual(ok.returncode, 0, ok.stdout)
            bad = subprocess.run([sys.executable, "-I", str(READ_DENY), "check", str(t / "events.jsonl"), str(t / "sessions"), '{"/a" = "deny", "/b" = "deny"}'], capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("1 of 2 denied paths", bad.stdout)


if __name__ == "__main__":
    unittest.main()
