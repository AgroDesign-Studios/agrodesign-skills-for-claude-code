#!/usr/bin/env python3
"""verdict.py — the /agrodesign-deep-review verdict, computed from the engine's result, fail-closed.

  python3 -I verdict.py <result.json> --head <sha> --expect-rows KEY[,KEY...]
                        [--rerun KEY=FILE ...]
                        [--gates FILE] [--expect-mechanical NAME[,NAME...]]
                        [--expect-judgment NAME[,NAME...]] [--gate-row KEY]
                        [--rulings FILE]

<result.json> is the return value of agrodesign-deep-review.workflow.js (the Workflow tool's
output; a {"result": ...} wrapper is accepted). --rerun KEY=FILE puts row KEY of a
re-run (a fresh Workflow call with only that row, on the same commit) in place of the
main result's row. The verdict is read from the result's bookkeeping BEFORE its
findings, because a row that never ran looks exactly like a clean row in the findings
alone:

  Do-not-merge       an unresolved Critical (confirmed by the Verify phase, left
                     unverified by the skeptic cap, or never verified at all)
  Incomplete         else: an expected row missing, failed, or answered on another
                     commit; a Critical/High left unverified, verified by fewer votes
                     than requested, or absent from the Verify phase; a result or a
                     re-run for another commit; an expected gate missing or
                     UNMEASURED — never a pass
  Merge-after-fixes  else: an unresolved High, or a gate FAIL
  Merge              else

"Unresolved": not refuted by a strict majority of the requested skeptic votes cast at
the required tier on the commit under review (the engine's `refuted`). A refutation
that came only from below tier is advisory; the orchestrator may rule on a finding by
hand with --rulings (a JSON list of {"key", "file", "title", "ruling": "refuted" |
"confirmed", "why"}; `why` is required and printed; a ruling must match exactly one
finding), never silently.

Gates: --gates FILE holds the project's gate script output, lines `GATE <name>
PASS|FAIL|N-A|UNMEASURED [note]` (mechanical gates, --expect-mechanical). Judgment
gates (--expect-judgment) are read only from the `gates` list of the row --gate-row
names (default diff-bugs): a gate another row reports, or a judgment gate standing in
for a mechanical one, does not count. Several reports of one gate combine to the worst
(FAIL, then UNMEASURED, then PASS/N-A). A missing gate is Incomplete, as is UNMEASURED;
a FAIL counts as an unresolved High.

Output: one line per fact (ROW, FINDING, UNVERIFIED, RULED, GATE), then
`VERDICT=<verdict>` and the three-line verdict block. Exit 0 Merge; 1 any other
verdict; 2 usage or an unreadable result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SEVERE = ("critical", "high")
GATE_LINE = re.compile(r"^GATE\s+(\S+)\s+(PASS|FAIL|N-A|UNMEASURED)\b\s*(.*)$")
GATE_RANK = {"FAIL": 3, "UNMEASURED": 2, "PASS": 1, "N-A": 1}
HEX_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"{path}: not readable JSON ({exc})")
        raise


def load_result(path: Path) -> dict[str, Any]:
    raw = load_json(path)
    result = raw.get("result", raw) if isinstance(raw, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        die(f"{path}: not an agrodesign-deep-review result (no results list)")
    assert isinstance(result, dict)
    return result


def one_line(text: object, limit: int = 160) -> str:
    """Model output shown as data: one line, control characters dropped, cut."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def finding_key(key: str, f: dict[str, Any]) -> tuple[str, str, str]:
    return (key, str(f.get("file") or ""), str(f.get("title") or ""))


def worse(a: str | None, b: str) -> str:
    return b if a is None or GATE_RANK.get(b, 3) > GATE_RANK.get(a, 3) else a


def merge_reruns(result: dict[str, Any], reruns: list[str], head: str) -> list[str]:
    """Put each --rerun KEY=FILE row in place of the main result's row (same commit only)."""
    notes = []
    for spec in reruns:
        key, _, fpath = spec.partition("=")
        if not key or not fpath:
            die(f"--rerun needs KEY=FILE, got {spec!r}")
        rd = load_result(Path(fpath))
        if rd.get("head") != head:
            die(f"--rerun {fpath} reviewed commit {one_line(rd.get('head'), 64)}, not {head}: re-run the row on the commit under review")
        hit = [r for r in rd["results"] if isinstance(r, dict) and r.get("key") == key]
        if len(hit) != 1:
            die(f"--rerun {fpath} holds {len(hit)} row(s) {key!r}, not one")
        result["results"] = [r for r in result["results"] if not (isinstance(r, dict) and r.get("key") == key)] + hit
        notes.append(f"RERUN {key} from {fpath}")
    return notes


def compute(
    result: dict[str, Any],
    head: str,
    expect_rows: list[str],
    gate_lines: list[str],
    expect_mechanical: list[str],
    expect_judgment: list[str],
    gate_row: str,
    rulings: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """(verdict, output lines)."""
    out: list[str] = []
    incomplete: list[str] = []
    criticals: list[str] = []
    highs: list[str] = []
    if str(result.get("head") or "") != head:
        incomplete.append(f"the result is for commit {one_line(result.get('head'), 64)}, not {head}")
    rows = [r for r in result.get("results") or [] if isinstance(r, dict)]
    keys = Counter(str(r.get("key")) for r in rows)
    for k, n in keys.items():
        if n > 1:
            incomplete.append(f"row {k} appears {n} times")
    for k in expect_rows:
        if k not in keys:
            incomplete.append(f"row {k} is not in the result")
            out.append(f"ROW {k} missing")
    # Every finding the rows report, by (row, file, title): a ruling must match exactly one.
    all_findings = Counter(finding_key(str(r.get("key")), f) for r in rows if isinstance(r.get("report"), dict) for f in r["report"].get("findings") or [] if isinstance(f, dict))
    ruled: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rulings:
        if not isinstance(r, dict) or r.get("ruling") not in ("refuted", "confirmed") or not str(r.get("why") or "").strip():
            die("--rulings: each entry needs key, file, title, ruling (refuted|confirmed) and a non-empty why")
        fk = (str(r.get("key")), str(r.get("file") or ""), str(r.get("title") or ""))
        if all_findings.get(fk, 0) != 1:
            die(f"--rulings: {fk} matches {all_findings.get(fk, 0)} findings, not exactly one")
        ruled[fk] = r
    judgment: dict[str, str] = {}
    for r in rows:
        key = str(r.get("key"))
        requested = f"{r.get('model')}/{r.get('effort')}"
        if r.get("failed") or r.get("head_seen") != head or not isinstance(r.get("report"), dict):
            why = r.get("reason") or ("head-mismatch" if r.get("head_seen") not in (None, head) else "no report")
            incomplete.append(f"row {key} gave no usable answer on {head[:12]} ({one_line(why, 40)})")
            out.append(f"ROW {key} FAILED reason={one_line(why, 40)} requested={requested} launched={r.get('launched')}")
            continue
        report = r["report"]
        out.append(f"ROW {key} ok requested={requested} launched={r.get('launched')} ran_on={one_line(r.get('ran_on'), 40)} findings={len(report.get('findings') or [])} unchecked={len(report.get('unchecked') or [])}")
        if key == gate_row:
            for g in report.get("gates") or []:
                if isinstance(g, dict) and g.get("gate"):
                    judgment[str(g["gate"])] = worse(judgment.get(str(g["gate"])), str(g.get("result") or "UNMEASURED"))
        # Every Critical/High the finder reported must come back from the Verify phase,
        # verified or listed as unverified: one that did neither was never checked.
        severe = Counter(finding_key(key, f) for f in report.get("findings") or [] if isinstance(f, dict) and f.get("severity") in SEVERE)
        accounted: Counter[tuple[str, str, str]] = Counter()
        for v in r.get("verdicts") or []:
            if not isinstance(v, dict) or not isinstance(v.get("finding"), dict):
                continue
            f = v["finding"]
            sev = str(f.get("severity") or "")
            fk = finding_key(key, f)
            accounted[fk] += 1
            label = f"{sev} {key} {one_line(f.get('file'), 120)}:{f.get('line', 0)} {one_line(f.get('title'))}"
            status = "refuted" if v.get("refuted") is True else "confirmed"
            if fk in ruled:
                status = str(ruled[fk]["ruling"])
                out.append(f"RULED {status} by the orchestrator: {label} — {one_line(ruled[fk]['why'], 300)}")
            elif int(v.get("votes_returned") or 0) < int(v.get("votes_requested") or 1):
                incomplete.append(f"skeptic votes missing for {label}")
            advisory = int(v.get("substituted_refutes") or 0) + int(v.get("tier_mismatch_refutes") or 0) + int(v.get("head_mismatch_refutes") or 0)
            note = f" (advisory refutes below tier or off-commit: {advisory})" if advisory and status == "confirmed" else ""
            out.append(f"FINDING {status} {label}{note}")
            if status == "confirmed" and sev in SEVERE:
                (criticals if sev == "critical" else highs).append(label)
        for f in r.get("unverified") or []:
            if isinstance(f, dict):
                sev = str(f.get("severity") or "")
                accounted[finding_key(key, f)] += 1
                label = f"{sev} {key} {one_line(f.get('file'), 120)}:{f.get('line', 0)} {one_line(f.get('title'))}"
                out.append(f"UNVERIFIED {label}")
                incomplete.append(f"unverified {label}")
                (criticals if sev == "critical" else highs).append(label)
        for fk, n in (severe - accounted).items():
            sev = next(str(f.get("severity")) for f in report.get("findings") or [] if isinstance(f, dict) and finding_key(key, f) == fk)
            label = f"{sev} {key} {one_line(fk[1], 120)} {one_line(fk[2])}"
            out.append(f"UNVERIFIED {label} (never reached the Verify phase{f', x{n}' if n > 1 else ''})")
            incomplete.append(f"never verified {label}")
            (criticals if sev == "critical" else highs).append(label)
    mechanical: dict[str, str] = {}
    for line in gate_lines:
        m = GATE_LINE.match(line.strip())
        if m:
            mechanical[m.group(1)] = worse(mechanical.get(m.group(1)), m.group(2))
    for kind, expected, source in (("mechanical", expect_mechanical, mechanical), ("judgment", expect_judgment, judgment)):
        for name in expected:
            res = source.get(name)
            if res is None:
                out.append(f"GATE {name} MISSING ({kind})")
                incomplete.append(f"{kind} gate {name} missing")
                continue
            out.append(f"GATE {name} {res} ({kind})")
            if res == "UNMEASURED":
                incomplete.append(f"{kind} gate {name} UNMEASURED")
            elif res == "FAIL":
                highs.append(f"gate {name} FAIL")
        for name in sorted(set(source) - set(expected)):
            out.append(f"GATE {name} {source[name]} ({kind}, not expected)")
    if criticals:
        verdict = "Do-not-merge"
    elif incomplete:
        verdict = "Incomplete"
    elif highs:
        verdict = "Merge-after-fixes"
    else:
        verdict = "Merge"
    concerns = criticals[:2] or highs[:2] or incomplete[:2]
    main = f"Critical {len(criticals)} / High {len(highs)}" + (f"; {len(incomplete)} coverage gap(s)" if incomplete else "") + (f" — {'; '.join(concerns)}" if concerns else "")
    action = {
        "Do-not-merge": "rethink or revert the change; fix every Critical before another round",
        "Incomplete": "re-run what did not run (rows, gates, unverified findings) on the same commit; this is not a pass",
        "Merge-after-fixes": f"merge after {len(highs)} fix(es) and a round that verifies them",
        "Merge": "merge as is",
    }[verdict]
    out += [f"VERDICT={verdict}", f"Verdict: {verdict}", f"Main concerns: {main}", f"Recommended action: {action}"]
    return verdict, out


def names(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verdict.py", description="the /agrodesign-deep-review verdict, fail-closed")
    p.add_argument("result", type=Path)
    p.add_argument("--head", required=True)
    p.add_argument("--expect-rows", required=True)
    p.add_argument("--rerun", action="append", default=[])
    p.add_argument("--gates", type=Path)
    p.add_argument("--expect-mechanical", default="")
    p.add_argument("--expect-judgment", default="")
    p.add_argument("--gate-row", default="diff-bugs")
    p.add_argument("--rulings", type=Path)
    a = p.parse_args(argv)
    if not HEX_ID.fullmatch(a.head):
        die("--head must be the full commit id under review")
    result = load_result(a.result)
    rows = names(a.expect_rows)
    if not rows:
        die("--expect-rows names no row")
    notes = merge_reruns(result, a.rerun, a.head)
    gate_lines = a.gates.read_text(encoding="utf-8").splitlines() if a.gates else []
    rulings = load_json(a.rulings) if a.rulings else []
    if not isinstance(rulings, list):
        die("--rulings must hold a JSON list")
    verdict, lines = compute(result, a.head, rows, gate_lines, names(a.expect_mechanical), names(a.expect_judgment), a.gate_row, rulings)
    print("\n".join(notes + lines))
    return 0 if verdict == "Merge" else 1


if __name__ == "__main__":
    sys.exit(main())
