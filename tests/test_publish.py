"""The publish half of github_upload_check.py against a made-up GitHub (a stub gh and a
local credential-free API): approvals bound to what they cover and used once, the push
that needs approval 1, verify after the push, the preflight before a switch, the switch and
the release that need approval 2, tag collisions, and resuming after a partial success."""

from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from helpers import FakeGitHub, UploadCase, fake_repo

REPO = "acme/widgets"

#: The made-up destination's post-receive hook: each ref update becomes an activity event.
POST_RECEIVE = r"""#!/usr/bin/env python3
import json, sys
path = "@STATE@"
st = json.load(open(path))
for line in sys.stdin:
    old, new, ref = line.split()
    kind = "branch_creation" if set(old) == {"0"} else ("branch_deletion" if set(new) == {"0"} else "push")
    st.setdefault("lists", {}).setdefault("activity", []).append({"activity_type": kind, "ref": ref, "before": old, "after": new})
json.dump(st, open(path, "w"), indent=1)
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Pat Example", "-c", "user.email=pat@example.com", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


class PublishCases(UploadCase):
    def setUp(self) -> None:
        super().setUp()
        self.dest = self.tmp / "dest.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.dest)], check=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True)
        (self.clone / "README.md").write_text("Made-up widgets.\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "First")
        git(self.clone, "remote", "add", "origin", str(self.dest))
        self.tip = git(self.clone, "rev-parse", "HEAD")
        self.title = self.tmp / "release" / "title.txt"
        self.title.parent.mkdir()
        self.title.write_text("Widgets v1.0.0\n")
        self.notes = self.tmp / "release" / "notes.md"
        self.notes.write_text("First release of the made-up widgets.\n")
        self.github = FakeGitHub(self.tmp / "gh")
        self.addCleanup(self.github.stop)
        self.github.update(lambda st: st["repos"].update({REPO: fake_repo(REPO, str(self.dest))}))
        self.env = {**self.env, "AGRODESIGN_GITHUB_PUBLISH_GH_BIN": str(self.github.gh), "AGRODESIGN_GITHUB_PUBLISH_API_URL": self.github.api_url, "AGRODESIGN_GITHUB_PUBLISH_RETRY_SECONDS": "0.05"}
        # Every push to the made-up destination lands in its activity log, as on GitHub.
        hook = self.dest / "hooks" / "post-receive"
        hook.write_text(POST_RECEIVE.replace("@STATE@", str(self.github.path)))
        hook.chmod(0o755)

    # ── helpers ──────────────────────────────────────────────────────────────
    def sources(self, whole: bool = True) -> list[str]:
        return ["--git", str(self.clone), "--push", "origin", "main", *(["--whole-history"] if whole else []),
                "--file", str(self.title), "--file", str(self.notes), "--repo-meta", REPO]  # fmt: skip

    def sealed(self, name: str = "run", whole: bool = True) -> Path:
        run = self.tmp / name
        got = self.run_check("stage", "--out", str(run), *self.sources(whole))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.complete(run)
        got = self.run_check("seal", "--run", str(run))
        self.assertIn("SEAL=ok", got.stdout, got.stdout + got.stderr)
        return run

    def approve(self, run: Path, step: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_check("approve", "--run", str(run), "--step", step, "--repo", REPO, "--words", f"owner says {step}", *extra)

    def release_args(self, target: str | None = None) -> list[str]:
        return ["--release-tag", "v1.0.0", "--release-target", target or self.tip, "--release-title", str(self.title), "--release-notes", str(self.notes)]

    def pushed(self, name: str = "run", whole: bool = True) -> Path:
        run = self.sealed(name, whole)
        self.assertEqual(self.approve(run, "push").returncode, 0)
        got = self.run_check("push", "--run", str(run), *self.sources(whole))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        return run

    def publish(self, run: Path) -> subprocess.CompletedProcess[str]:
        return self.run_check("publish", "--run", str(run), "--repo", REPO, "--release-title", str(self.title), "--release-notes", str(self.notes))

    def dest_refs(self) -> str:
        return subprocess.run(["git", "--git-dir", str(self.dest), "for-each-ref"], capture_output=True, text=True, check=True).stdout

    def visibility(self) -> str:
        return str(self.github.read()["repos"][REPO]["visibility"])

    # ── approval 1 and the push ──────────────────────────────────────────────
    def test_push_without_approval_is_refused(self) -> None:
        run = self.sealed()
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUSH=refused: no push approval", got.stdout)
        self.assertEqual(self.dest_refs(), "")

    def test_approval_records_the_words_and_what_they_cover(self) -> None:
        run = self.sealed()
        got = self.approve(run, "push")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        rec = json.loads((run / "approvals" / "push.json").read_text())
        self.assertEqual(rec["owner_words"], "owner says push")
        self.assertEqual(rec["repo"]["id"], 4242)
        self.assertEqual(rec["digest"], self.digest(run))
        self.assertEqual(rec["pushes"], [{"url": str(self.dest), "ref": "refs/heads/main", "tip": self.tip}])
        self.assertIsNone(rec["used"])
        self.assertIn("an open push approval exists", self.approve(run, "push").stdout)

    def test_push_then_verify_is_same_and_the_approval_is_used_once(self) -> None:
        run = self.pushed()
        self.assertEqual(git(self.dest, "rev-parse", "main"), self.tip)
        got = self.run_check("verify", "--run", str(run), *self.sources())
        self.assertIn("VERIFY=same", got.stdout, got.stdout + got.stderr)
        self.assertEqual(json.loads((run / "approvals" / "push.json").read_text())["used"]["result"], "pushed")
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("approval was used", got.stdout)

    def test_verify_after_a_push_without_whole_history_is_same(self) -> None:
        run = self.pushed(whole=False)
        got = self.run_check("verify", "--run", str(run), *self.sources(whole=False))
        self.assertIn("VERIFY=same", got.stdout, got.stdout + got.stderr)

    def test_expired_approval_is_refused(self) -> None:
        run = self.sealed()
        self.approve(run, "push", "--valid-minutes", "5")
        path = run / "approvals" / "push.json"
        rec = json.loads(path.read_text())
        rec["expires_at"] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        path.write_text(json.dumps(rec))
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("expired", got.stdout)
        self.assertEqual(self.dest_refs(), "")

    def test_revoked_approval_is_refused(self) -> None:
        run = self.sealed()
        self.approve(run, "push")
        got = self.run_check("revoke", "--run", str(run), "--step", "push", "--words", "stop for now")
        self.assertEqual(got.returncode, 0)
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("revoked", got.stdout)
        self.assertEqual(self.dest_refs(), "")
        self.assertEqual(json.loads((run / "approvals" / "push.json").read_text())["revoked"]["words"], "stop for now")

    def test_approval_for_another_repository_is_refused(self) -> None:
        run = self.sealed()
        self.github.update(lambda st: st["repos"].update({"acme/other": fake_repo("acme/other", "/nowhere/other.git", 77)}))
        got = self.run_check("approve", "--run", str(run), "--step", "push", "--repo", "acme/other", "--words", "yes")
        self.assertEqual(got.returncode, 1)
        self.assertIn("which is not acme/other", got.stdout)
        self.approve(run, "push")
        self.github.update(lambda st: st["repos"][REPO].update({"id": 9999}))  # deleted and made again under the same name
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("id 4242", got.stdout)
        self.assertEqual(self.dest_refs(), "")

    def test_push_refused_when_the_destination_lost_the_base(self) -> None:
        first = self.pushed("first", whole=False)
        self.assertTrue(first.exists())
        (self.clone / "more.txt").write_text("more\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "Second")
        run = self.sealed("second", whole=False)
        self.approve(run, "push")
        subprocess.run(["git", "--git-dir", str(self.dest), "update-ref", "-d", "refs/heads/main"], check=True)
        got = self.run_check("push", "--run", str(run), *self.sources(whole=False))
        self.assertEqual(got.returncode, 1)
        self.assertIn("no longer reaches", got.stdout)

    # ── the preflight ────────────────────────────────────────────────────────
    def test_preflight_is_ok_for_a_new_private_repository(self) -> None:
        run = self.pushed()
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PREFLIGHT=ok", got.stdout)
        self.assertIn("INVENTORY refs", got.stdout)

    def test_preflight_refuses_content_nobody_checked(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st["lists"].update({"issues": [{"number": 1}], "labels": [{"name": "bug"}, {"name": "zeta-internal"}]}))
        self.github.update(lambda st: st["repos"][REPO].update({"has_wiki": True}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("issues_and_pull_requests: 1", got.stdout)
        self.assertIn("labels beyond GitHub's defaults", got.stdout)
        self.assertIn("the wiki is on", got.stdout)

    def test_preflight_refuses_a_read_that_failed(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st.update({"fail": [f"repos/{REPO}/milestones"]}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("could not read milestones", got.stdout)

    def test_preflight_refuses_history_the_run_did_not_read(self) -> None:
        run = self.pushed(whole=False)
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("without --whole-history", got.stdout)

    def test_preflight_refuses_an_unchecked_ref_and_changed_metadata(self) -> None:
        run = self.pushed()
        git(self.clone, "push", "-q", "origin", "main:refs/heads/side")
        self.github.update(lambda st: st["repos"][REPO].update({"description": "changed after the check"}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("refs/heads/side", got.stdout)
        self.assertIn("description, homepage or topics changed", got.stdout)

    # ── approval 2, the switch and the release ───────────────────────────────
    def test_publish_needs_approval_two(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("no publish approval", got.stdout)
        self.assertEqual(self.visibility(), "private")

    def test_publish_switches_releases_and_checks_without_login(self) -> None:
        run = self.pushed()
        self.assertEqual(self.run_check("preflight", "--run", str(run), "--repo", REPO).returncode, 0)
        got = self.approve(run, "publish", *self.release_args())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PUBLISH=ok", got.stdout)
        self.assertIn(f"RELEASE=ok v1.0.0 at {self.tip}", got.stdout)
        self.assertIn("PUBLIC=ok", got.stdout)
        self.assertEqual(self.visibility(), "public")
        create = next(a for a in self.github.read()["log"] if a[:2] == ["release", "create"])
        self.assertIn("--target", create)
        self.assertEqual(create[create.index("--target") + 1], self.tip)
        self.assertNotIn("--generate-notes", create)
        self.assertNotIn("--draft", create)
        self.assertNotIn("--prerelease", create)
        self.assertEqual(json.loads((run / "approvals" / "publish.json").read_text())["used"]["result"], "published+released")
        again = self.publish(run)  # a second publish has nothing left to do: no second switch or release
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("PUBLISH=ok", again.stdout)
        self.assertNotIn("RELEASE=ok", again.stdout)
        log = self.github.read()["log"]
        self.assertEqual(sum(1 for a in log if a[:2] == ["repo", "edit"]), 1)
        self.assertEqual(sum(1 for a in log if a[:2] == ["release", "create"]), 1)

    def test_publish_refused_when_the_destination_changed_after_the_preflight(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        self.github.update(lambda st: st["lists"].update({"issues": [{"number": 7}]}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("issues_and_pull_requests: 1", got.stdout)
        self.assertEqual(self.visibility(), "private")

    def test_publish_refuses_release_files_other_than_the_approved(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        other = self.tmp / "release" / "other.md"
        other.write_text("Different notes.\n")
        got = self.run_check("publish", "--run", str(run), "--repo", REPO, "--release-title", str(self.title), "--release-notes", str(other))
        self.assertEqual(got.returncode, 1)
        self.assertIn("not the approved release notes", got.stdout)
        self.assertEqual(self.visibility(), "private")

    def test_release_target_must_be_a_pushed_commit(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        got = self.approve(run, "publish", *self.release_args("0" * 40))
        self.assertEqual(got.returncode, 2)
        self.assertIn("--release-target", got.stderr)

    def test_a_tag_pointing_elsewhere_stops_the_release(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        self.github.update(lambda st: st.update({"tag_refs": {"v1.0.0": "1" * 40}}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("RELEASE=refused: tag v1.0.0 already exists and points at 111111111111", got.stdout)
        self.assertFalse(any(a[:2] == ["release", "create"] for a in self.github.read()["log"]))

    def test_resume_after_the_release_failed(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=ok", got.stdout)
        self.assertIn("RELEASE=failed", got.stdout)
        self.assertEqual(self.visibility(), "public")
        self.github.update(lambda st: st.update({"release_fail": False}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("PUBLISH=ok", got.stdout)  # the switch is not repeated
        self.assertIn("RELEASE=ok", got.stdout)
        edits = [a for a in self.github.read()["log"] if a[:2] == ["repo", "edit"]]
        self.assertEqual(len(edits), 1)

    def test_resume_when_the_switch_happened_but_was_not_recorded(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        state = json.loads((run / "state.json").read_text())
        state["publishing"] = {"at": "2026-01-02T03:04:05+00:00", "repo_id": 4242}
        (run / "state.json").write_text(json.dumps(state))
        self.github.update(lambda st: st["repos"][REPO].update({"private": False, "visibility": "public"}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("RELEASE=ok", got.stdout)
        self.assertEqual(json.loads((run / "state.json").read_text())["published"]["note"], "found public after an interrupted run, checked")

    def test_repository_metadata_is_staged_and_required(self) -> None:
        run = self.sealed()
        meta = next(e for e in json.loads((run / "manifest.json").read_text())["entries"] if e["origin"] == f"repo:meta:{REPO}")
        self.assertEqual(json.loads((run / "tree" / meta["path"]).read_text())["description"], "Made-up skills for tests")
        bare = self.tmp / "bare-run"
        self.run_check("stage", "--out", str(bare), "--git", str(self.clone), "--push", "origin", "main", "--whole-history")
        self.complete(bare)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(bare)).stdout)
        self.run_check("approve", "--run", str(bare), "--step", "push", "--repo", REPO, "--words", "yes")
        self.assertEqual(self.run_check("push", "--run", str(bare), "--git", str(self.clone), "--push", "origin", "main", "--whole-history").returncode, 0)
        got = self.run_check("preflight", "--run", str(bare), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("add --repo-meta", got.stdout)

    def test_publish_approval_needs_a_clean_preflight(self) -> None:
        run = self.pushed()
        got = self.approve(run, "publish", *self.release_args())
        self.assertEqual(got.returncode, 1)
        self.assertIn("run preflight first", got.stdout)

    def test_a_release_approved_after_the_switch(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(self.approve(run, "publish").returncode, 0)  # the switch only
        got = self.run_check("publish", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("RELEASE", got.stdout)
        self.assertEqual(self.approve(run, "release", *self.release_args()).returncode, 0)
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("RELEASE=ok v1.0.0", got.stdout)
        self.assertEqual(json.loads((run / "approvals" / "release.json").read_text())["used"]["result"], "released")

    def test_an_expired_publish_approval_needs_a_release_approval_to_finish(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish", *self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        self.assertIn("RELEASE=failed", self.publish(run).stdout)
        self.github.update(lambda st: st.update({"release_fail": False}))
        path = run / "approvals" / "publish.json"
        rec = json.loads(path.read_text())
        rec["expires_at"] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        path.write_text(json.dumps(rec))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("RELEASE=refused: the publish approval expired", got.stdout)
        self.assertEqual(self.approve(run, "release", *self.release_args()).returncode, 0)
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("RELEASE=ok", got.stdout)

    def test_a_release_on_a_repository_that_is_already_public(self) -> None:
        self.github.update(lambda st: st["repos"][REPO].update({"private": False, "visibility": "public"}))
        run = self.pushed(whole=False)
        self.assertIn("run preflight first", self.approve(run, "publish").stdout)
        got = self.approve(run, "release", *self.release_args())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("PUBLISH=ok", got.stdout)
        self.assertIn("RELEASE=ok v1.0.0", got.stdout)
        self.assertFalse(any(a[:2] == ["repo", "edit"] for a in self.github.read()["log"]))

    # ── the independent review's cases ───────────────────────────────────────
    def ready_to_publish(self, *release: str) -> Path:
        run = self.pushed()
        self.assertEqual(self.run_check("preflight", "--run", str(run), "--repo", REPO).returncode, 0)
        got = self.approve(run, "publish", *release)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        return run

    def test_a_revoked_publish_approval_stops_the_switch(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.run_check("revoke", "--run", str(run), "--step", "publish", "--words", "not yet")
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("revoked", got.stdout)
        self.assertEqual(self.visibility(), "private")

    def test_a_revoked_release_approval_is_not_replaced_by_approval_two(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        self.publish(run)
        self.github.update(lambda st: st.update({"release_fail": False}))
        self.approve(run, "release", *self.release_args())
        self.run_check("revoke", "--run", str(run), "--step", "release", "--words", "no release")
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("RELEASE=refused: the release approval was revoked", got.stdout)
        self.assertEqual(self.github.read()["lists"].get("releases", []), [])

    def test_an_approval_for_another_seal_is_refused(self) -> None:
        run = self.sealed()
        self.approve(run, "push")
        sealed = (run / "SEALED").read_text().split()
        (run / "SEALED").write_text(f"{sealed[0]} {sealed[1]} 2026-01-01T00:00:00+00:00\n")  # same check, a new seal
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("the seal changed since the approval", got.stdout)

    def test_an_approval_naming_other_pushes_is_refused(self) -> None:
        run = self.sealed()
        self.approve(run, "push")
        path = run / "approvals" / "push.json"
        rec = json.loads(path.read_text())
        rec["pushes"][0]["tip"] = "2" * 40
        path.write_text(json.dumps(rec))
        got = self.run_check("push", "--run", str(run), *self.sources())
        self.assertEqual(got.returncode, 1)
        self.assertIn("other pushes", got.stdout)

    def test_a_stale_preflight_cannot_be_approved(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        pf = json.loads((run / "preflight.json").read_text())
        pf["digest"] = "3" * 64
        (run / "preflight.json").write_text(json.dumps(pf))
        self.assertIn("run preflight first", self.approve(run, "publish").stdout)

    def test_preflight_reads_every_page_and_every_kind(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st["lists"].update({"issues": [{"number": n} for n in range(150)], "forks": [{"id": 1}]}))
        self.github.update(lambda st: st.update({"pages": True, "fail": [f"repos/{REPO}/actions/runs"]}))
        self.github.update(lambda st: st["repos"][REPO].update({"has_discussions": True}))
        git(self.clone, "push", "-q", "origin", "main:refs/pull/1/head")
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        for text in ("issues_and_pull_requests: 150", "forks: 1", "Pages site exists", "could not read actions_runs", "discussions are on", "refs/pull/1/head"):
            self.assertIn(text, got.stdout)

    def test_preflight_needs_approval_one(self) -> None:
        run = self.sealed()
        git(self.clone, "push", "-q", "origin", "main")  # pushed by hand, not by this skill
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("approval 1 was not given and used", got.stdout)

    def test_a_failed_switch_resumes(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"edit_fail": True}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=failed", got.stdout)
        self.assertEqual(self.visibility(), "private")
        self.github.update(lambda st: st.update({"edit_fail": False}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PUBLISH=ok", got.stdout)

    def test_a_change_during_the_switch_switches_back(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"edit_adds_issue": True}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=reverted", got.stdout)
        self.assertEqual(self.visibility(), "private")
        self.assertEqual(self.github.read()["lists"].get("releases", []), [])

    def test_an_existing_tag_at_the_target_is_used(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"tag_refs": {"v1.0.0": self.tip}}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("RELEASE=ok", got.stdout)

    def test_a_release_someone_else_made_is_refused(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        other = {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "name": "Not ours", "body": "x", "assets": []}
        self.github.update(lambda st: st.update({"tag_refs": {"v1.0.0": self.tip}}))
        self.github.update(lambda st: st["lists"].update({"releases": [other]}))
        self.github.update(lambda st: st.update({"fail": []}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("releases: 1", got.stdout)  # the preflight before the switch already sees it
        self.assertEqual(self.visibility(), "private")

    def test_a_release_that_appeared_after_the_switch_is_refused(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        self.publish(run)
        self.github.update(lambda st: st.update({"release_fail": False, "tag_refs": {"v1.0.0": self.tip}}))
        other = {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "name": "Not ours", "body": "x", "assets": []}
        self.github.update(lambda st: st["lists"].update({"releases": [other]}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("already exists and differs: its title is not the approved one", got.stdout)

    def test_a_draft_for_the_tag_stops_the_release(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        self.publish(run)
        self.github.update(lambda st: st.update({"release_fail": False}))
        self.github.update(lambda st: st["lists"].update({"releases": [{"tag_name": "v1.0.0", "draft": True, "assets": []}]}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("a draft release for v1.0.0 exists", got.stdout)

    def test_no_release_after_the_repository_went_back_to_private(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"release_fail": True}))
        self.publish(run)
        self.github.update(lambda st: st.update({"release_fail": False}))
        self.github.update(lambda st: st["repos"][REPO].update({"private": True, "visibility": "private"}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("is not public now", got.stdout)
        self.assertEqual(self.github.read()["lists"].get("releases", []), [])

    def test_assets_are_uploaded_from_the_staged_copies(self) -> None:
        asset = self.tmp / "release" / "widgets.zip"
        asset.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        run = self.tmp / "run"
        got = self.run_check("stage", "--out", str(run), *self.sources(), "--file", str(asset))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.complete(run)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(run)).stdout)
        self.approve(run, "push")
        self.assertEqual(self.run_check("push", "--run", str(run), *self.sources(), "--file", str(asset)).returncode, 0)
        self.assertEqual(self.run_check("preflight", "--run", str(run), "--repo", REPO).returncode, 0)
        self.assertEqual(self.approve(run, "publish", *self.release_args(), "--release-asset", str(asset)).returncode, 0)
        asset.write_bytes(b"changed after the approval")  # the upload uses the run's own copy
        got = self.run_check("publish", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        uploaded = self.github.read()["uploaded"]
        self.assertEqual([Path(u).name for u in uploaded], ["widgets.zip"])
        rel = self.github.read()["lists"]["releases"][0]
        self.assertEqual(rel["assets"], [{"name": "widgets.zip", "size": 22}])

    # ── the second review round's cases ──────────────────────────────────────
    def test_a_switch_that_reported_failure_is_checked_on_resume(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"edit_fail_after_switch": True}))
        got = self.publish(run)
        self.assertIn("PUBLISH=failed", got.stdout)
        self.assertEqual(self.visibility(), "public")
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("checked now", got.stdout)
        self.assertIn("RELEASE=ok", got.stdout)

    def test_a_change_found_on_resume_switches_back(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"edit_fail_after_switch": True}))
        self.publish(run)
        self.github.update(lambda st: st["lists"].update({"issues": [{"number": 5}]}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=reverted", got.stdout)
        self.assertEqual(self.visibility(), "private")
        self.assertEqual(self.github.read()["lists"].get("releases", []), [])

    def test_a_failed_switch_back_blocks_the_run(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"edit_adds_issue": True, "edit_private_fail": True}))
        got = self.publish(run)
        self.assertIn("the switch back FAILED", got.stdout)
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("could NOT switch it back", got.stdout)
        self.assertEqual(self.github.read()["lists"].get("releases", []), [])

    def test_a_wiki_read_that_fails_refuses(self) -> None:
        run = self.pushed()
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO, env={**self.env, "GIT_ALLOW_PROTOCOL": "ext"})
        self.assertEqual(got.returncode, 1)
        self.assertIn("could not read the wiki repository", got.stdout)

    def test_an_asset_approved_under_another_name_is_refused(self) -> None:
        asset = self.tmp / "release" / "widgets.zip"
        asset.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        run = self.tmp / "run"
        self.run_check("stage", "--out", str(run), *self.sources(), "--file", str(asset))
        self.complete(run)
        self.assertIn("SEAL=ok", self.run_check("seal", "--run", str(run)).stdout)
        self.approve(run, "push")
        self.assertEqual(self.run_check("push", "--run", str(run), *self.sources(), "--file", str(asset)).returncode, 0)
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        twin = self.tmp / "release" / "renamed.zip"
        twin.write_bytes(asset.read_bytes())
        got = self.approve(run, "publish", *self.release_args(), "--release-asset", str(twin))
        self.assertEqual(got.returncode, 2)
        self.assertIn("under that name", got.stderr)

    def test_a_revoke_during_the_preflight_stops_the_switch(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        path = str(run / "approvals" / "publish.json")
        self.github.update(lambda st: st.update({"api_calls": 0, "revoke_file": path, "revoke_after_calls": 3}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=refused: the publish approval was revoked", got.stdout)
        self.assertEqual(self.visibility(), "private")

    # ── the third review round's cases ───────────────────────────────────────
    def test_resume_that_reads_private_again_records_nothing(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        state = json.loads((run / "state.json").read_text())
        state["publishing"] = {"at": "2026-01-02T03:04:05+00:00", "repo_id": 4242}
        (run / "state.json").write_text(json.dumps(state))
        self.github.update(lambda st: st["repos"][REPO].update({"private": False, "visibility": "public"}))
        self.github.update(lambda st: st.update({"api_calls": 0, "private_at_call": 2}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("PUBLISH=unconfirmed", got.stdout)
        self.assertNotIn("published", json.loads((run / "state.json").read_text()))
        self.assertIsNone(json.loads((run / "approvals" / "publish.json").read_text())["used"])

    def test_a_read_failure_after_the_switch_reverts_and_a_fresh_approval_continues(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        self.github.update(lambda st: st.update({"fail_when_public": [f"repos/{REPO}/milestones"]}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("could not be read right after the switch", got.stdout)
        self.assertEqual(self.visibility(), "private")
        self.assertIn("record a new publish approval", self.publish(run).stdout)
        self.github.update(lambda st: st.update({"fail_when_public": []}))
        self.assertEqual(self.run_check("preflight", "--run", str(run), "--repo", REPO).returncode, 0)
        self.assertEqual(self.approve(run, "publish", *self.release_args()).returncode, 0)
        got = self.publish(run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PUBLISH=ok", got.stdout)
        self.assertIn("RELEASE=ok", got.stdout)

    def test_an_asset_name_github_would_change_is_refused(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        odd = self.tmp / "release" / "my widgets#1.zip"
        odd.write_bytes(b"x")
        got = self.approve(run, "publish", *self.release_args(), "--release-asset", str(odd))
        self.assertEqual(got.returncode, 2)
        self.assertIn("rename", got.stderr)

    def test_activity_other_than_plain_pushes_is_refused(self) -> None:
        run = self.pushed()
        events = [{"activity_type": "branch_creation", "ref": "refs/heads/main", "before": "0" * 40, "after": self.tip},
                  {"activity_type": "force_push", "ref": "refs/heads/main", "before": "4" * 40, "after": self.tip},
                  {"activity_type": "push", "ref": "refs/heads/main", "before": self.tip, "after": "5" * 40}]  # fmt: skip
        self.github.update(lambda st: st["lists"].update({"activity": events}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("activity: a force_push on refs/heads/main", got.stdout)
        self.assertIn("outside this run's checked history", got.stdout)
        self.github.update(lambda st: st["lists"].update({"activity": events[:1]}))
        self.assertEqual(self.run_check("preflight", "--run", str(run), "--repo", REPO).returncode, 0)

    def test_statuses_on_a_checked_commit_and_an_old_repository_are_refused(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st["lists"].update({"statuses": [{"state": "success", "context": "ci"}]}))
        self.github.update(lambda st: st["totals"].update({"check-runs": 1}))
        self.github.update(lambda st: st["repos"][REPO].update({"created_at": "2020-01-02T03:04:05Z"}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("1 status(es) and 1 check run(s)", got.stdout)
        self.assertIn("activity log covers 90 days", got.stdout)

    # ── from the in-house copy's list ────────────────────────────────────────
    def test_an_approval_replaced_mid_run_stops_the_switch(self) -> None:
        run = self.ready_to_publish(*self.release_args())
        path = str(run / "approvals" / "publish.json")
        self.github.update(lambda st: st.update({"api_calls": 0, "replace_file": path, "replace_after_calls": 3}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("was replaced while this command ran", got.stdout)
        self.assertEqual(self.visibility(), "private")

    def test_preflight_waits_for_the_activity_log(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st["lists"].update({"activity": []}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("does not show this run's push", got.stdout)

    def test_packages_and_a_custom_social_image_are_refused(self) -> None:
        run = self.pushed()
        self.github.update(lambda st: st.update({"graphql": {"packages": {"totalCount": 2}, "usesCustomOpenGraphImage": True}}))
        got = self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.assertEqual(got.returncode, 1)
        self.assertIn("packages: 2", got.stdout)
        self.assertIn("custom social preview image", got.stdout)

    def test_asset_names_that_end_in_a_dot_or_differ_only_in_case(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        dotted = self.tmp / "release" / "widgets."
        dotted.write_bytes(b"x")
        got = self.approve(run, "publish", *self.release_args(), "--release-asset", str(dotted))
        self.assertEqual(got.returncode, 2)
        self.assertIn("rename", got.stderr)
        (self.tmp / "release" / "a").mkdir()
        one, two = self.tmp / "release" / "Widgets.zip", self.tmp / "release" / "a" / "widgets.zip"
        one.write_bytes(b"1")
        two.write_bytes(b"2")
        got = self.approve(run, "publish", *self.release_args(), "--release-asset", str(one), "--release-asset", str(two))
        self.assertEqual(got.returncode, 2)
        self.assertIn("without case", got.stderr)

    def test_url_spellings_are_one_repository(self) -> None:
        import importlib.util
        import sys

        from helpers import CHECK

        spec = importlib.util.spec_from_file_location("guc_t", CHECK)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["guc_t"] = mod
        spec.loader.exec_module(mod)
        same = {mod.norm_url(u) for u in ("https://github.com/Acme/Widgets.git", "git@github.com:acme/widgets.git", "ssh://git@github.com/acme/widgets", "https://github.com/acme/widgets/")}
        self.assertEqual(same, {"https://github.com/acme/widgets"})
        self.assertNotEqual(mod.norm_url("https://github.com/acme/widgets2"), "https://github.com/acme/widgets")

    def test_a_repository_made_public_elsewhere_is_refused(self) -> None:
        run = self.pushed()
        self.run_check("preflight", "--run", str(run), "--repo", REPO)
        self.approve(run, "publish")
        self.github.update(lambda st: st["repos"][REPO].update({"private": False, "visibility": "public"}))
        got = self.publish(run)
        self.assertEqual(got.returncode, 1)
        self.assertIn("already public, and not by this run", got.stdout)


if __name__ == "__main__":
    unittest.main()
