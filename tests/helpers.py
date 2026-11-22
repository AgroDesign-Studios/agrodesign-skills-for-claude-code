"""Shared fixtures for the portability tests: fictitious values only.

Every secret-shaped string is built at runtime, so no scanner reads a literal one in
this repository; every host name, account and path is made up.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPLOAD = ROOT / "skills" / "agrodesign-github-upload" / "scripts"
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


def executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UploadCase(unittest.TestCase):
    """A temporary folder (outside every home and git work tree) with a stub gitleaks
    and a fictitious values file at $XDG_CONFIG_HOME/agrodesign-github-upload/values.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gu-test-", dir=self._base())
        self.tmp = Path(self._tmp.name)
        self.gitleaks = executable(self.tmp / "gitleaks", GITLEAKS_STUB)
        self.config = self.tmp / "config"
        self.values_path = self.config / "agrodesign-github-upload" / "values.json"
        self.write_values({"host.build": FAKE_HOST, "account.ci": FAKE_ACCOUNT})
        self.env = {**os.environ, "AGRODESIGN_GITHUB_UPLOAD_GITLEAKS_BIN": str(self.gitleaks), "XDG_CONFIG_HOME": str(self.config)}

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
