"""github_upload_check.py on fictitious values: staging, the scan, the values file's
guarantees (hash recorded, a change withdraws the seal, a missing file stops the seal),
the gates, the seal and a push of the checked commit to a local bare repository."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import unittest
import zipfile
from pathlib import Path

from helpers import FAKE_ACCOUNT, FAKE_HOST, FAKE_KEY_LINE, FAKE_TOKEN, FakeGitHub, UploadCase, fake_repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Pat Example", "-c", "user.email=pat@example.com", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


class ScanCases(UploadCase):
    def test_clean_tree_is_clean_and_records_the_values_file(self) -> None:
        run = self.staged(self.make_tree({"README.md": "A public readme.\n", "src/app.py": "print('hello')\n"}))
        got = self.run_check("scan", "--run", str(run))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("RESULT=CLEAN", got.stdout)
        self.assertIn("VALUES checked (2 value(s) from", got.stdout)
        report = json.loads((run / "scan" / "report.json").read_text())
        self.assertEqual(report["values"], "checked")
        self.assertEqual(len(report["values_sha256"]), 64)
        self.assertEqual(report["value_count"], 2)

    def test_private_value_hits_by_key_and_is_never_printed(self) -> None:
        run = self.staged(self.make_tree({"notes.md": f"Deploy to {FAKE_HOST.upper()} as {FAKE_ACCOUNT}.\n"}))
        got = self.run_check("scan", "--run", str(run))
        self.assertEqual(got.returncode, 1)
        self.assertIn("private_value value host.build", got.stdout)  # matched case-insensitively
        self.assertIn("private_value value account.ci", got.stdout)
        report = (run / "scan" / "report.json").read_text()
        for out in (got.stdout, report):
            self.assertNotIn(FAKE_HOST, out.lower())
            self.assertNotIn(FAKE_ACCOUNT, out)

    def test_value_in_a_file_name_and_an_archive_member(self) -> None:
        src = self.make_tree({f"{FAKE_ACCOUNT}.txt": "x\n"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dir/", "")
            zf.writestr("dir/inner.txt", f"host {FAKE_HOST}\n")
        (src / "bundle.zip").write_bytes(buf.getvalue())
        run = self.staged(src)
        got = self.run_check("scan", "--run", str(run))
        self.assertIn("name:private_value value account.ci", got.stdout)
        self.assertIn("archive:private_value value host.build", got.stdout)
        self.assertNotIn("archive_member_unread", got.stdout, "a directory entry is not an unread member")

    def test_tar_link_target_is_scanned(self) -> None:
        src = self.make_tree({})
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo("link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = f"/home/{FAKE_ACCOUNT}/secret"
            tf.addfile(ti)
        (src / "a.tar").write_bytes(buf.getvalue())
        got = self.run_check("scan", "--run", str(self.staged(src)))
        self.assertIn("archive:private_value value account.ci", got.stdout)

    def test_value_hidden_by_a_shift_jis_decoding_is_still_found(self) -> None:
        src = self.make_tree({})
        (src / "latin1.txt").write_bytes(b"Andr\xe9" + FAKE_ACCOUNT.encode() + b"\n")  # cp932 swallows the byte after 0xe9
        got = self.run_check("scan", "--run", str(self.staged(src)))
        self.assertIn("raw-ascii:private_value value account.ci", got.stdout)

    def test_secret_shapes_are_redacted(self) -> None:
        run = self.staged(self.make_tree({"conf.txt": f"token = {FAKE_TOKEN}\n{FAKE_KEY_LINE}\n"}))
        got = self.run_check("scan", "--run", str(run))
        self.assertIn("github_token", got.stdout)
        self.assertIn("private_key", got.stdout)
        self.assertNotIn(FAKE_TOKEN, got.stdout + (run / "scan" / "report.json").read_text())

    def test_relative_run_path_works(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        got = subprocess.run(["python3", "-I", str(Path(__file__).resolve().parent.parent / "skills/agrodesign-github-publish/scripts/github_upload_check.py"), "scan", "--run", run.name], cwd=self.tmp, env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def test_home_directory_run_is_refused(self) -> None:
        got = self.run_check("stage", "--out", str(Path.home() / "gu-test-never-created"), "--tree", str(self.make_tree({"a": "b"})))
        self.assertEqual(got.returncode, 2)
        self.assertIn("home directory", got.stderr)
        self.assertFalse((Path.home() / "gu-test-never-created").exists())

    def test_staged_tree_change_is_refused(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        f = next((run / "tree").rglob("a.txt"))
        f.chmod(0o644)
        f.write_text("changed\n")
        got = self.run_check("scan", "--run", str(run))
        self.assertEqual(got.returncode, 2)
        self.assertIn("TREE=changed", got.stderr)


class ValuesFileCases(UploadCase):
    def scan_problem(self) -> str:
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        self.complete(run)
        got = self.run_check("seal", "--run", str(run))
        self.assertEqual(got.returncode, 1, got.stdout)
        return got.stdout

    def test_missing_values_file_stops_the_seal(self) -> None:
        self.values_path.unlink()
        out = self.scan_problem()
        self.assertIn("private values were not checked (missing", out)

    def test_empty_values_file_stops_the_seal(self) -> None:
        self.write_values({"word.short": {"value": "ab", "scan": False}})
        self.assertIn("private values were not checked (empty)", self.scan_problem())

    def test_values_file_inside_a_git_work_tree_is_refused(self) -> None:
        git(self.config, "init", "-q")  # the values file now lies inside a work tree
        self.assertIn("in-git", self.scan_problem())

    def test_config_folder_linked_into_a_work_tree_is_refused(self) -> None:
        dotfiles = self.tmp / "dotfiles"
        (dotfiles / "agrodesign-github-publish").mkdir(parents=True)
        git(dotfiles, "init", "-q")
        (dotfiles / "agrodesign-github-publish" / "values.json").write_text(self.values_path.read_text())
        self.values_path.unlink()
        self.values_path.parent.rmdir()
        self.values_path.parent.symlink_to(dotfiles / "agrodesign-github-publish")
        self.assertIn("in-git", self.scan_problem())

    def test_relative_xdg_config_home_is_ignored(self) -> None:
        env = {**self.env, "XDG_CONFIG_HOME": "relative/config"}
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        got = self.run_check("scan", "--run", str(run), env=env)
        self.assertNotIn("relative/config", got.stdout + got.stderr)

    def test_symlinked_values_file_is_refused(self) -> None:
        real = self.tmp / "real-values.json"
        real.write_text(self.values_path.read_text())
        self.values_path.unlink()
        self.values_path.symlink_to(real)
        self.assertIn("symbolic link", self.scan_problem())

    def test_invalid_values_are_refused_without_echoing_them(self) -> None:
        self.write_values({"host.build": " padded-" + FAKE_HOST})
        out = self.scan_problem()
        self.assertIn("surrounding blanks", out)
        self.assertNotIn(FAKE_HOST, out)

    def test_values_file_trial_is_refused_at_seal(self) -> None:
        trial = self.tmp / "trial.json"
        trial.write_text(json.dumps({"format": 1, "values": {"one.value": "only-this-one"}}))
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        got = self.run_check("scan", "--run", str(run), "--values-file", str(trial))
        self.assertIn("a --values-file trial; seal refuses this run", got.stdout)
        self.complete(run)  # scans again without the trial ...
        got = self.run_check("scan", "--run", str(run), "--values-file", str(trial))  # ... and back to the trial
        self.bind(run, self.resolved)
        got = self.run_check("seal", "--run", str(run))
        self.assertIn("not the values file", got.stdout)

    def test_seal_is_withdrawn_when_the_values_change(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "fine\n"}))
        self.complete(run)
        got = self.run_check("seal", "--run", str(run))
        self.assertEqual(got.returncode, 0, got.stdout)
        self.assertIn("SEAL=ok", got.stdout)
        src = self.tmp / "src"
        got = self.run_check("verify", "--run", str(run), "--tree", str(src))
        self.assertIn("VERIFY=same", got.stdout)
        self.write_values({"host.build": FAKE_HOST, "account.ci": FAKE_ACCOUNT, "host.new": "new-" + FAKE_HOST})
        got = self.run_check("verify", "--run", str(run), "--tree", str(src))
        self.assertEqual(got.returncode, 1)
        self.assertIn("VERIFY=withdrawn", got.stdout)
        self.assertIn("changed since the scan", got.stdout)
        got = self.run_check("seal", "--run", str(run))
        self.assertEqual(got.returncode, 1)
        self.assertFalse((run / "SEALED").exists(), "a refused seal deletes the earlier one")


class GateAndSealCases(UploadCase):
    def test_gate_before_codex_needs_every_hit_cleared(self) -> None:
        run = self.staged(self.make_tree({"notes.md": f"see {FAKE_HOST}\n"}))
        self.through_claude(run)
        got = self.run_check("gate", "--run", str(run), "--before", "codex")
        self.assertEqual(got.returncode, 1)
        self.assertIn("unresolved ['scan#0']", got.stdout)
        self.resolved = {"scan#0": "false because the fixture host is made up"}
        self.bind(run, self.resolved)
        got = self.run_check("gate", "--run", str(run), "--before", "codex")
        self.assertEqual(got.returncode, 0, got.stdout)
        self.assertIn("GATE=open before codex", got.stdout)

    def test_gate_refuses_an_answer_that_names_too_few_files(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "one\n", "b.txt": "two\n"}))
        self.through_claude(run)
        answer = json.loads((run / "stage2-claude.json").read_text())
        answer["read"] = answer["read"][:1]
        (run / "stage2-claude.json").write_text(json.dumps(answer))
        self.bind(run, self.resolved)
        got = self.run_check("gate", "--run", str(run), "--before", "codex")
        self.assertIn("named in neither read nor not_read", got.stdout)

    def test_gate_refuses_a_claude_answer_not_on_opus(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "one\n"}))
        self.through_claude(run)
        answer = json.loads((run / "stage2-claude.json").read_text())
        answer["ran_on"] = "Haiku 4.5"
        (run / "stage2-claude.json").write_text(json.dumps(answer))
        self.bind(run, self.resolved)
        self.assertIn("no ran_on naming Opus", self.run_check("gate", "--run", str(run), "--before", "codex").stdout)

    def test_seal_refuses_a_confirmed_high_and_a_malformed_grok_answer(self) -> None:
        run = self.staged(self.make_tree({"a.txt": "one\n"}))
        self.complete(run)
        final = json.loads((run / "stage5-final.json").read_text())
        final["confirmed"] = [{"severity": "high", "lens": "B", "file": "a.txt", "line": 1}]
        (run / "stage5-final.json").write_text(json.dumps(final))
        self.assertIn("confirmed finding that is critical, high", self.run_check("seal", "--run", str(run)).stdout)
        final["confirmed"] = []
        (run / "stage5-final.json").write_text(json.dumps(final))
        grok = next(run.glob("stage4-grok-*.json"))
        g = json.loads(grok.read_text())
        g["findings"] = 5  # a truthy non-list: refused, never a crash
        grok.write_text(json.dumps(g))
        got = self.run_check("seal", "--run", str(run))
        self.assertEqual(got.returncode, 1)
        self.assertNotIn("Traceback", got.stderr)

    def test_waiving_a_stage_never_skips_the_scan_clearance(self) -> None:
        run = self.staged(self.make_tree({"notes.md": f"see {FAKE_HOST}\n"}))
        self.complete(run, {"scan#0": "false because the fixture host is made up"})
        verdict = json.loads((run / "verdict.json").read_text())
        verdict["stages"]["codex"] = {"status": "waived", "owner_words": "fixture waiver"}
        (run / "verdict.json").write_text(json.dumps(verdict))
        for f in run.glob("stage3-codex-*"):
            f.unlink()
        self.resolved = {}
        self.bind(run, self.resolved)
        self.assertIn("unresolved ['scan#0']", self.run_check("seal", "--run", str(run)).stdout)

    def test_seal_withdrawn_by_a_changed_answer(self) -> None:
        src = self.make_tree({"a.txt": "one\n"})
        run = self.staged(src)
        self.complete(run)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(run)).stdout)
        (run / "triage.md").write_text("edited after the seal\n")
        got = self.run_check("verify", "--run", str(run), "--tree", str(src))
        self.assertIn("VERIFY=withdrawn", got.stdout)


class GitPushCases(UploadCase):
    def setUp(self) -> None:
        super().setUp()
        self.dest = self.tmp / "dest.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.dest)], check=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True)
        (self.clone / "README.md").write_text("Public.\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "First")
        git(self.clone, "remote", "add", "origin", str(self.dest))

    def test_push_sends_exactly_the_checked_commit(self) -> None:
        run = self.tmp / "run"
        got = self.run_check("stage", "--out", str(run), "--git", str(self.clone), "--push", "origin", "main")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("IDENTITY Pat Example <pat@example.com>", got.stdout)
        tip = git(self.clone, "rev-parse", "HEAD")
        self.assertIn(f"PUSH {self.dest} refs/heads/main at {tip}", got.stdout)
        self.complete(run)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(run)).stdout)
        github = FakeGitHub(self.tmp / "gh")
        self.addCleanup(github.stop)
        github.update(lambda st: st["repos"].update({"acme/widgets": fake_repo("acme/widgets", str(self.dest))}))
        env = {**self.env, "AGRODESIGN_GITHUB_PUBLISH_GH_BIN": str(github.gh)}
        got = self.run_check("push", "--run", str(run), "--git", str(self.clone), "--push", "origin", "main", env=env)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUSH=refused: no push approval", got.stdout)
        got = self.run_check("approve", "--run", str(run), "--step", "push", "--repo", "acme/widgets", "--words", "push it", env=env)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        got = self.run_check("push", "--run", str(run), "--git", str(self.clone), "--push", "origin", "main", env=env)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PUSH=ok", got.stdout)
        self.assertEqual(subprocess.run(["git", "--git-dir", str(self.dest), "rev-parse", "main"], capture_output=True, text=True, check=True).stdout.strip(), tip)

    def test_a_commit_after_the_check_is_not_pushed(self) -> None:
        run = self.tmp / "run"
        self.run_check("stage", "--out", str(run), "--git", str(self.clone), "--push", "origin", "main")
        self.complete(run)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(run)).stdout)
        (self.clone / "late.txt").write_text(f"{FAKE_HOST}\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "Late")
        got = self.run_check("push", "--run", str(run), "--git", str(self.clone), "--push", "origin", "main")
        self.assertEqual(got.returncode, 1)
        self.assertIn("VERIFY=different", got.stdout)
        refs = subprocess.run(["git", "--git-dir", str(self.dest), "for-each-ref"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(refs, "", "nothing was pushed")

    def test_credentials_in_the_url_are_refused(self) -> None:
        url = "https://" + "user" + ":" + "pw" + "@example.com/repo.git"
        git(self.clone, "remote", "set-url", "origin", url)
        got = self.run_check("stage", "--out", str(self.tmp / "run"), "--git", str(self.clone), "--push", "origin", "main")
        self.assertEqual(got.returncode, 2)
        self.assertIn("credentials", got.stderr)
        self.assertNotIn("pw@", got.stderr)


if __name__ == "__main__":
    unittest.main()
