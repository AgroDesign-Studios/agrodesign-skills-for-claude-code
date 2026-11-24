"""The agrodesign-deep-review engine (run under node with a stand-in Workflow runtime), its
schemas, and the fail-closed verdict and full-sweep record scripts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REVIEW

ENGINE = REVIEW / "agrodesign-deep-review.workflow.js"
HARNESS = Path(__file__).resolve().parent / "workflow_harness.mjs"
VERDICT = REVIEW / "scripts" / "verdict.py"
REPORT = REVIEW / "scripts" / "full_sweep_report.py"
HEAD = "a" * 40
OTHER = "b" * 40


def report(findings: list[dict[str, object]] | None = None, head: str = "$HEAD", ran_on: str = "Opus 5.5") -> dict[str, object]:
    return {"findings": findings or [], "fp_candidates": [], "unchecked": [], "head_seen": head, "ran_on": ran_on}


def finding(sev: str = "high", title: str = "Missing owner check", file: str = "src/api/items.py") -> dict[str, object]:
    return {"severity": sev, "title": title, "file": file, "line": 10, "issue": "any user can edit any item", "confidence": "High"}


def verdict(refuted: bool, head: str = "$HEAD", ran_on: str = "Opus 5.5") -> dict[str, object]:
    return {"refuted": refuted, "reasoning": "read the code", "confidence": 0.9, "head_seen": head, "ran_on": ran_on}


def base_args(**over: object) -> dict[str, object]:
    args: dict[str, object] = {"mode": "pr", "head": HEAD, "rubric": "/abs/review-rubric.md", "agents": [{"key": "diff-bugs", "model": "opus", "effort": "high", "brief": "Review the diff."}], "votes": 1}
    args.update(over)
    return args


@unittest.skipUnless(shutil.which("node"), "node is needed to run the workflow engine")
class EngineCases(unittest.TestCase):
    def run_engine(self, args: dict[str, object], rules: list[dict[str, object]]) -> dict[str, object]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"args": args, "rules": rules}, fh)
        try:
            got = subprocess.run(["node", str(HARNESS), str(ENGINE), fh.name], capture_output=True, text=True, check=False, timeout=60)
        finally:
            Path(fh.name).unlink()
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    def test_meta_and_schemas(self) -> None:
        src = ENGINE.read_text()
        self.assertRegex(src, r"(?m)^export const meta = \{")
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report()}])
        find = out["calls"][0]
        self.assertEqual(find["agentType"], "Explore")
        for field in ("findings", "fp_candidates", "unchecked", "head_seen", "ran_on"):
            self.assertIn(field, find["required"])
        self.assertEqual(find["promptHead"], "PROBE SAFETY. ")
        self.assertTrue(find["hasHead"])
        # The rules name whom they are for and claim no authority over the rest of the prompt.
        self.assertNotIn("whatever the rest of this prompt says", src)
        self.assertIn("skill gives these rules to every agent it starts", src)

    def test_rows_without_a_tier_or_commit_are_rejected(self) -> None:
        for args, msg in (
            (base_args(agents=[{"key": "x", "model": "opus", "brief": "b"}]), "missing key/model/effort/brief"),
            (base_args(agents=[{"key": "x", "model": "gpt", "effort": "high", "brief": "b"}]), "model must be one of"),
            (base_args(head="HEAD"), "args.head must be the full commit id"),
            (base_args(mode="weekly"), "args.mode"),
            (base_args(rubric=""), "args.rubric"),
            (base_args(agents=[{"key": "x", "model": "opus", "effort": "high", "brief": "b"}] * 2), "appears twice"),
        ):
            with self.subTest(msg=msg):
                out = self.run_engine(args, [])
                self.assertIn(msg, str(out["error"]))
                self.assertEqual(out["calls"], [])

    def test_refuted_at_tier_on_the_commit(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding()])}, {"label": "^verify:", "value": verdict(True)}])
        row = out["result"]["results"][0]
        self.assertTrue(row["verdicts"][0]["refuted"])
        skeptic = [c for c in out["calls"] if c["label"].startswith("verify:")][0]
        self.assertEqual((skeptic["model"], skeptic["effort"]), ("opus", "high"))
        self.assertIn("head_seen", skeptic["required"])

    def test_skeptic_on_another_commit_cannot_refute(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding()])}, {"label": "^verify:", "value": verdict(True, head=OTHER)}])
        v = out["result"]["results"][0]["verdicts"][0]
        self.assertFalse(v["refuted"])
        self.assertEqual(v["head_mismatch_refutes"], 1)

    def test_finder_on_another_commit_is_a_failed_row(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding()], head=OTHER)}])
        self.assertEqual(out["result"]["failed"], ["diff-bugs"])
        self.assertEqual(out["result"]["head_mismatch"], ["diff-bugs"])
        self.assertFalse(any(c["label"].startswith("verify:") for c in out["calls"]), "a finding from another tree is never verified")

    def test_critical_or_top_tier_finding_gets_a_top_tier_skeptic(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding("critical")])}, {"label": "^verify:", "value": verdict(True, ran_on="Fable 5.2")}])
        skeptic = [c for c in out["calls"] if c["label"].startswith("verify:")][0]
        self.assertEqual((skeptic["model"], skeptic["effort"]), ("fable", "max"))
        self.assertTrue(out["result"]["results"][0]["verdicts"][0]["refuted"])

    def test_opus_skeptic_that_ran_on_a_lower_family_cannot_refute(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding()])}, {"label": "^verify:", "value": verdict(True, ran_on="Sonnet 5.5")}])
        v = out["result"]["results"][0]["verdicts"][0]
        self.assertFalse(v["refuted"])
        self.assertEqual(v["tier_mismatch_refutes"], 1)

    def test_opus_skeptic_that_names_no_model_cannot_refute(self) -> None:
        for ran_on in ("unknown", "a large language model"):
            with self.subTest(ran_on=ran_on):
                out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding()])}, {"label": "^verify:", "value": verdict(True, ran_on=ran_on)}])
                v = out["result"]["results"][0]["verdicts"][0]
                self.assertFalse(v["refuted"])
                self.assertEqual(v["tier_mismatch_refutes"], 1)

    def test_switched_top_tier_skeptic_cannot_refute(self) -> None:
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report([finding("critical")])}, {"label": "^verify:", "value": verdict(True, ran_on="Fable 5.2, switched to Opus 5.5")}])
        v = out["result"]["results"][0]["verdicts"][0]
        self.assertFalse(v["refuted"])
        self.assertEqual(v["tier_mismatch_refutes"], 1)

    def test_top_tier_finder_that_returns_nothing_is_rerun_on_opus(self) -> None:
        args = base_args(agents=[{"key": "diff-security", "model": "fable", "effort": "max", "brief": "Security."}])
        out = self.run_engine(args, [
            {"label": "^find:diff-security$", "model": "fable", "reject": "weekly usage limit reached"},
            {"label": "^find:diff-security:opus-fallback$", "value": report([finding()])},
            {"label": "^verify:", "value": verdict(True)},
        ])
        res = out["result"]
        self.assertEqual(res["substituted"][0]["reason"], "rejected")
        self.assertIn("usage limit", res["fable_out"])
        row = res["results"][0]
        self.assertEqual(row["launched"], "opus/max")
        v = row["verdicts"][0]
        self.assertFalse(v["refuted"], "the skeptic's fable call went to opus (fable-out): its refute is below tier")
        self.assertEqual(v["substituted_refutes"], 1)

    def test_dead_row_is_failed_never_clean(self) -> None:
        out = self.run_engine(base_args(), [])
        self.assertEqual(out["result"]["failed"], ["diff-bugs"])

    def test_skeptic_cap_leaves_the_rest_unverified(self) -> None:
        many = [finding(title=f"f{k}") for k in range(22)]
        out = self.run_engine(base_args(), [{"label": "^find:", "value": report(many)}, {"label": "^verify:", "value": verdict(False)}])
        row = out["result"]["results"][0]
        self.assertEqual(len(row["verdicts"]), 20)
        self.assertEqual(len(row["unverified"]), 2)

    def test_project_probe_rules_are_added(self) -> None:
        out = self.run_engine(base_args(probe_rules_extra="The database here is production."), [{"label": "^find:", "value": report()}])
        self.assertTrue(out["calls"][0]["hasExtra"])


def engine_result(rows: list[dict[str, object]], head: str = HEAD) -> dict[str, object]:
    return {"mode": "pr", "head": head, "results": rows, "failed": [r["key"] for r in rows if r.get("failed")], "substituted": [], "errors": []}


def ok_row(key: str, verdicts: list[dict[str, object]] | None = None, unverified: list[dict[str, object]] | None = None, gates: list[dict[str, object]] | None = None) -> dict[str, object]:
    rep = {"findings": [v["finding"] for v in verdicts or []], "fp_candidates": [], "unchecked": [], "head_seen": HEAD, "ran_on": "Opus 5.5"}
    if gates:
        rep["gates"] = gates
    return {"key": key, "model": "opus", "effort": "high", "launched": "opus/high", "ran_on": "Opus 5.5", "head_seen": HEAD, "report": rep, "verdicts": verdicts or [], "unverified": unverified or []}


def v(sev: str, refuted: bool, title: str = "t") -> dict[str, object]:
    return {"finding": finding(sev, title), "refuted": refuted, "votes_requested": 1, "votes_returned": 1, "substituted_refutes": 0, "tier_mismatch_refutes": 0, "head_mismatch_refutes": 0}


class VerdictCases(unittest.TestCase):
    def verdict(self, result: dict[str, object], rows: str = "diff-bugs", *extra: str, gates: str | None = None, rulings: list[object] | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "r.json").write_text(json.dumps({"result": result}))
            args = [sys.executable, "-I", str(VERDICT), str(t / "r.json"), "--head", HEAD, "--expect-rows", rows, *extra]
            if gates is not None:
                (t / "gates.txt").write_text(gates)
                args += ["--gates", str(t / "gates.txt")]
            if rulings is not None:
                (t / "rulings.json").write_text(json.dumps(rulings))
                args += ["--rulings", str(t / "rulings.json")]
            got = subprocess.run(args, capture_output=True, text=True, check=False)
        m = re.search(r"^VERDICT=(\S+)$", got.stdout, re.M)
        return got.returncode, (m.group(1) if m else got.stderr)

    def test_clean_is_merge(self) -> None:
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs")])), (0, "Merge"))

    def test_confirmed_high_and_critical(self) -> None:
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", [v("high", False)])]))[1], "Merge-after-fixes")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", [v("critical", False), v("high", True)])]))[1], "Do-not-merge")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", [v("high", True)])]))[1], "Merge")

    def test_coverage_gaps_are_incomplete(self) -> None:
        failed = {"key": "diff-security", "model": "fable", "effort": "max", "failed": True, "reason": "nothing"}
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs"), failed]), "diff-bugs,diff-security")[1], "Incomplete")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs")]), "diff-bugs,diff-security")[1], "Incomplete", "a missing row")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", unverified=[finding("high")])]))[1], "Incomplete")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", unverified=[finding("critical")])]))[1], "Do-not-merge")
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs")], head="c" * 40))[1], "Incomplete", "another commit")

    def test_gates(self) -> None:
        r = engine_result([ok_row("diff-bugs", gates=[{"gate": "changelog-describes-change", "result": "PASS"}])])
        exp = ("--expect-judgment", "changelog-describes-change", "--expect-mechanical", "number-unique")
        self.assertEqual(self.verdict(r, "diff-bugs", *exp, gates="GATE number-unique PASS\n")[1], "Merge")
        self.assertEqual(self.verdict(r, "diff-bugs", *exp, gates="GATE number-unique FAIL taken\n")[1], "Merge-after-fixes")
        self.assertEqual(self.verdict(r, "diff-bugs", *exp, gates="GATE number-unique UNMEASURED\n")[1], "Incomplete")
        self.assertEqual(self.verdict(r, "diff-bugs", *exp, gates="")[1], "Incomplete", "a missing gate")
        self.assertEqual(self.verdict(r, "diff-bugs", *exp, gates="GATE number-unique FAIL taken\nGATE number-unique PASS\n")[1], "Merge-after-fixes", "the worst report of a gate wins")

    def test_judgment_gates_come_from_the_gate_row_only(self) -> None:
        r = engine_result([
            ok_row("diff-bugs", gates=[{"gate": "changelog", "result": "FAIL"}]),
            ok_row("diff-security", gates=[{"gate": "changelog", "result": "PASS"}, {"gate": "number-unique", "result": "PASS"}]),
        ])
        self.assertEqual(self.verdict(r, "diff-bugs,diff-security", "--expect-judgment", "changelog")[1], "Merge-after-fixes")
        self.assertEqual(self.verdict(r, "diff-bugs,diff-security", "--expect-mechanical", "number-unique")[1], "Incomplete", "a judgment report never stands in for a mechanical gate")

    def test_a_severe_finding_that_never_reached_the_skeptics(self) -> None:
        crit = ok_row("diff-bugs")
        crit["report"]["findings"] = [finding("critical")]
        self.assertEqual(self.verdict(engine_result([crit]))[1], "Do-not-merge")
        high = ok_row("diff-bugs")
        high["report"]["findings"] = [finding("high")]
        self.assertEqual(self.verdict(engine_result([high]))[1], "Incomplete")

    def test_missing_skeptic_votes_are_incomplete(self) -> None:
        vv = v("high", True)
        vv.update({"votes_requested": 3, "votes_returned": 1, "refuted": False})
        self.assertEqual(self.verdict(engine_result([ok_row("diff-bugs", [vv])]))[1], "Incomplete")

    def test_rerun_replaces_a_failed_row_on_the_same_commit_only(self) -> None:
        failed = {"key": "diff-security", "model": "fable", "effort": "max", "failed": True, "reason": "nothing"}
        main = engine_result([ok_row("diff-bugs"), failed])
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps(engine_result([ok_row("diff-security")])))
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(engine_result([ok_row("diff-security")], head=OTHER)))
            self.assertEqual(self.verdict(main, "diff-bugs,diff-security", "--rerun", f"diff-security={good}")[1], "Merge")
            code, err = self.verdict(main, "diff-bugs,diff-security", "--rerun", f"diff-security={bad}")
            self.assertEqual(code, 2)
            self.assertIn("not " + HEAD, err)

    def test_rulings_need_a_reason_and_are_printed(self) -> None:
        r = engine_result([ok_row("diff-bugs", [v("high", False, title="t")])])
        key = {"key": "diff-bugs", "file": "src/api/items.py", "title": "t"}
        self.assertEqual(self.verdict(r, rulings=[{**key, "ruling": "refuted"}])[0], 2)
        self.assertEqual(self.verdict(r, rulings=[{**key, "ruling": "refuted", "why": "the parent router checks ownership"}])[1], "Merge")
        twice = engine_result([ok_row("diff-bugs", [v("high", False, title="t"), v("high", False, title="t")])])
        self.assertEqual(self.verdict(twice, rulings=[{**key, "ruling": "refuted", "why": "x"}])[0], 2, "a ruling must match exactly one finding")


class FullSweepReportCases(unittest.TestCase):
    def test_render_then_prev_round_trip(self) -> None:
        rows = [ok_row("a1-access-control", [v("high", False, title="IDOR on items | edit")]), {"key": "a7-async-concurrency", "model": "fable", "effort": "max", "failed": True}]
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "main.json").write_text(json.dumps(engine_result(rows)))
            got = subprocess.run([sys.executable, "-I", str(REPORT), "check", str(t / "main.json"), "--expect", "full"], capture_output=True, text=True, check=False)
            self.assertIn("failed[]      : ['a7-async-concurrency']", got.stdout)
            self.assertIn("verdict hint  : Incomplete", got.stdout)
            self.assertIn("missing: ['a10-bundle-deps'", got.stdout)
            got = subprocess.run([sys.executable, "-I", str(REPORT), "render", str(t / "main.json"), "--out-dir", str(t / "logs"), "--summary", str(t / "summary.md")], capture_output=True, text=True, check=False)
            self.assertEqual(got.returncode, 0, got.stderr)
            prev = json.loads(subprocess.run([sys.executable, "-I", str(REPORT), "prev", str(t / "summary.md")], capture_output=True, text=True, check=True).stdout)
            self.assertEqual(prev["blocking"][0]["title"], "IDOR on items | edit")
            self.assertEqual(prev["blocking"][0]["row"], "a1-access-control")
            self.assertEqual(prev["unchecked_by_row"]["a7-async-concurrency"], "row FAILED — nothing read")

    def test_rerun_on_another_commit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "main.json").write_text(json.dumps(engine_result([ok_row("a1-access-control")])))
            (t / "rerun.json").write_text(json.dumps(engine_result([ok_row("a1-access-control")], head=OTHER)))
            got = subprocess.run([sys.executable, "-I", str(REPORT), "check", str(t / "main.json"), "--rerun", f"a1-access-control={t / 'rerun.json'}"], capture_output=True, text=True, check=False)
            self.assertNotEqual(got.returncode, 0)
            self.assertIn("not the main run's", got.stderr)

    def test_phase_map_matches_the_rubric_table(self) -> None:
        rubric = (REVIEW / "rubric" / "review-rubric.md").read_text()
        keys = re.findall(r"^\s*\| (a\d+[ab]?-[a-z0-9-]+) \|", rubric, re.M)
        spec = __import__("importlib.util").util.spec_from_file_location("fsr", REPORT)
        mod = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(sorted(keys), sorted(mod.PHASE))
        briefs = (REVIEW / "rubric" / "full-sweep-briefs.md").read_text()
        self.assertEqual(sorted(re.findall(r"^### (a\d+[ab]?-[a-z0-9-]+) ", briefs, re.M)), sorted(mod.PHASE))


if __name__ == "__main__":
    unittest.main()
