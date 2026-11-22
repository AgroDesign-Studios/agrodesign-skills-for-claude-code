"""Repository hygiene: every skill is well formed, every script parses, the licence is
in place, and no tracking number of another repository has slipped into the published text."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT

TEXT_SUFFIXES = {".md", ".py", ".sh", ".js", ".mjs", ".json", ".txt", ""}
#: Bare issue or pull-request numbers ("#1234"), which point into some other repository's
#: tracker and mean nothing to a reader of this one. A publisher adds its own ticket
#: shapes, one regular expression per line, in HYGIENE_EXTRA_PATTERNS (kept outside git).
INTERNAL_IDS = [re.compile(r"(?<![\w&/])#\d{3,}\b")] + [re.compile(p) for p in os.environ.get("HYGIENE_EXTRA_PATTERNS", "").splitlines() if p.strip()]


def repo_files() -> list[Path]:
    return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.relative_to(ROOT).parts and "__pycache__" not in p.parts]


class HygieneCases(unittest.TestCase):
    def test_skills_have_frontmatter_named_after_their_folder(self) -> None:
        skills = sorted((ROOT / "skills").iterdir())
        self.assertEqual([s.name for s in skills], ["agrodesign-deep-review", "agrodesign-github-upload"])
        for skill in skills:
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            m = re.match(r"---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n", text, re.S)
            self.assertIsNotNone(m, skill)
            assert m is not None
            self.assertEqual(m.group(1), skill.name)
            self.assertGreater(len(m.group(2)), 80)

    def test_scripts_parse(self) -> None:
        for p in repo_files():
            if p.suffix == ".py":
                compile(p.read_text(encoding="utf-8"), str(p), "exec")  # parses, writes nothing
            elif p.suffix == ".sh":
                self.assertEqual(subprocess.run(["bash", "-n", str(p)], check=False).returncode, 0, p)
            elif p.suffix in (".js", ".mjs") and shutil.which("node"):
                # The engine is a Workflow body (top-level return), so it is checked as a function body.
                if p.name == "agrodesign-deep-review.workflow.js":
                    body = p.read_text().replace("export const meta", "const meta")
                    probe = "new (Object.getPrototypeOf(async function(){}).constructor)(require('fs').readFileSync(0, 'utf8'))"
                    self.assertEqual(subprocess.run(["node", "-e", probe], input=body, text=True, check=False).returncode, 0, p)
                else:
                    self.assertEqual(subprocess.run(["node", "--check", str(p)], check=False).returncode, 0, p)

    def test_licence(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 AgroDesign Studios", text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", text)

    def test_no_internal_tracking_numbers(self) -> None:
        hits = []
        for p in repo_files():
            if p.suffix not in TEXT_SUFFIXES:
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if p.name == Path(__file__).name and ("re.compile" in line or line.startswith("#:")):
                    continue
                if any(rx.search(line) for rx in INTERNAL_IDS):
                    hits.append(f"{p.relative_to(ROOT)}:{n}")
        self.assertEqual(hits, [])

    def test_scripts_are_executable(self) -> None:
        for p in (ROOT / "skills").rglob("*"):
            if p.suffix in (".py", ".sh") and p.parent.name == "scripts":
                self.assertTrue(p.stat().st_mode & 0o100, p)


if __name__ == "__main__":
    unittest.main()
