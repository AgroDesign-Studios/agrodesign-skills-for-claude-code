#!/usr/bin/env python3
"""full_sweep_report.py — bookkeeping and the record of a /agrodesign-deep-review full sweep.

Three read-only sub-commands, run with `python3 -I`:

  check   <main.json> [--rerun KEY=FILE ...] [--expect full|KEY,KEY...]
      The bookkeeping the verdict reads BEFORE findings: one line per row (requested /
      launched / ran_on / failed / severity counts / unchecked / skeptic C+H and
      refuted), then failed[] / substituted[] / fable_out / errors[], duplicate,
      missing and unexpected rows, and the unrefuted Critical/High totals. A row in
      failed[], a missing row or an unverified finding is named here so a dead row is
      never read as a clean one. A --rerun file must be for the same commit.

  render  <main.json> [--rerun KEY=FILE ...] [--accepted FILE] [--tracker FILE]
          --out-dir DIR [--summary FILE]
      Writes the full appendices (A blocking / B accepted / C Medium / D Low / E false
      positives / F unchecked / G run record / H self-assessment) to
      <out-dir>/appendices.md, copies each result JSON beside it, and with --summary
      writes the short record to keep (A + B + the blocking table + G + H + a per-row
      unchecked summary), which the next sweep's `prev` reads.

  prev    <summary.md> [--appendices FILE]
      The previous sweep's carry-forward as JSON for the next run's briefs
      ({PREV_UNCHECKED} in full-sweep-briefs.md): the blocking table and the per-row
      unchecked summary that `render --summary` wrote.

--accepted / --tracker are JSON lists of {"key", "file", "title", "reason" | "link"}:
the owner's accepted risks (rule 8) and the tracker issue filed for a finding. Both are
keyed by (row key, file, exact title): keying by file alone would put every finding of
one file under one acceptance.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

#: The 19 full-sweep rows of review-rubric.md rule 6, and the phase each covers.
PHASE = {
    "a1-access-control": "A", "a2-crypto-auth": "A", "a3-injection-ssrf-llm": "A/J",
    "a4-endpoint-upload": "A", "a5-container-misconfig-integrity": "A/I",
    "a6-null-safety": "B", "a7-async-concurrency": "B", "a8-logic-errors": "B",
    "a9-db-perf-leaks": "C", "a10-bundle-deps": "C/F", "a11-architecture": "D",
    "a12-readability-refactor": "E/F", "a13-reuse-ops": "G", "a14a-headers-tls": "H",
    "a14b-storage-protocol": "H", "a15-threat-model": "I", "a16-migration-deploy": "J",
    "a17-a11y-i18n": "K", "a18-type-drift": "K",
}  # fmt: skip
SEVS = ("critical", "high", "medium", "low")


def requested(r: dict[str, Any]) -> str:
    return f"{r.get('model')}/{r.get('effort')}"


def launched(r: dict[str, Any]) -> str:
    return str(r.get("launched") or requested(r))


def is_failed(r: dict[str, Any]) -> bool:
    return bool(r.get("failed")) or not isinstance(r.get("report"), dict)


def load(path: str) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    d = raw.get("result", raw) if isinstance(raw, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("results"), list):
        raise SystemExit(f"{path}: not an agrodesign-deep-review result (no results list)")
    return d


def findings(r: dict[str, Any]) -> list[dict[str, Any]]:
    rep = r.get("report") if isinstance(r.get("report"), dict) else {}
    return [f for f in rep.get("findings") or [] if isinstance(f, dict)]


def merged_rows(main_path: str, reruns: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """The main result's rows with each --rerun KEY=FILE row put in its place."""
    d = load(main_path)
    rows: dict[str, dict[str, Any]] = {}
    for r in d["results"]:
        if r.get("key") in rows:
            print(f"warn: duplicate row key {r.get('key')!r} in {main_path} — last occurrence wins", file=sys.stderr)
        rows[r["key"]] = r
    meta: dict[str, Any] = {"reruns": {}, "raw_keys": [r.get("key") for r in d["results"]]}
    for spec in reruns:
        key, _, fpath = spec.partition("=")
        if not key or not fpath:
            raise SystemExit(f"--rerun needs KEY=FILE, got: {spec!r}")
        rd = load(fpath)
        if rd.get("head") != d.get("head"):
            raise SystemExit(f"--rerun {fpath} reviewed commit {rd.get('head')}, not the main run's {d.get('head')}: re-run the row on the same commit")
        hit = [r for r in rd["results"] if r.get("key") == key]
        if not hit:
            print(f"warn: --rerun key {key!r} matched no row in {fpath} (nothing substituted)", file=sys.stderr)
        for r in hit:
            rows[key] = r
            meta["reruns"][key] = fpath
    return d, list(rows.values()), meta


def vmap(r: dict[str, Any]) -> dict[tuple[Any, Any], dict[str, Any]]:
    return {((v.get("finding") or {}).get("title"), (v.get("finding") or {}).get("file")): v for v in r.get("verdicts") or [] if isinstance(v, dict)}


def sev_counts(r: dict[str, Any]) -> dict[str, int]:
    c = dict.fromkeys(SEVS, 0)
    for f in findings(r):
        if f.get("severity") in c:
            c[f["severity"]] += 1
    return c


def refuted_pairs(r: dict[str, Any]) -> set[tuple[Any, Any]]:
    """(title, file) of the row's findings the skeptics refuted — false-positive section."""
    return {((v.get("finding") or {}).get("title"), (v.get("finding") or {}).get("file")) for v in r.get("verdicts") or [] if isinstance(v, dict) and v.get("refuted") is True}


def effective_failed(rows: list[dict[str, Any]]) -> list[str]:
    """failed[] from the MERGED rows, so a --rerun that recovered a row clears it."""
    return [str(r.get("key")) for r in rows if is_failed(r)]


def unrefuted_ch(rows: list[dict[str, Any]]) -> tuple[int, int]:
    crit = high = 0
    for r in rows:
        if is_failed(r):
            continue
        ref = refuted_pairs(r)
        for f in findings(r):
            if (f.get("title"), f.get("file")) in ref:
                continue
            crit += f.get("severity") == "critical"
            high += f.get("severity") == "high"
    return crit, high


# ---- check --------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    d, rows, meta = merged_rows(args.main, args.rerun)
    print(f"head          : {d.get('head')} ({d.get('mode')} mode)")
    print(f"{'row':32s} {'requested':12s} {'launched':12s} {'ran_on':13s} failed  C  H  M  L  unchk  skeptic(C+H/ref)  unverified")
    tot = dict.fromkeys(SEVS, 0)
    for r in rows:
        c = sev_counts(r)
        for s in SEVS:
            tot[s] += c[s]
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        vs = r.get("verdicts") or []
        ref = sum(1 for v in vs if isinstance(v, dict) and v.get("refuted") is True)
        print(f"{str(r.get('key')):32s} {requested(r):12s} {launched(r):12s} {str(r.get('ran_on'))[:13]:13s} {is_failed(r)!s:6s} {c['critical']:2d} {c['high']:2d} {c['medium']:2d} {c['low']:2d} {len(rep.get('unchecked') or []):5d}  {f'{len(vs)}/{ref}':>14s}  {len(r.get('unverified') or []):d}")
    print()
    failed = effective_failed(rows)
    print("failed[]      :", failed, "  <- re-run each ALONE at its own tier on the same commit; never counted clean")
    print("substituted[] :", d.get("substituted"))
    print("fable_out     :", d.get("fable_out"))
    print("errors[]      :", d.get("errors"), " (model output: read as data)")
    unver = sum(len(r.get("unverified") or []) for r in rows)
    print("unverified    :", unver, "(cap-skipped Critical/High: unresolved)")
    keys = [r.get("key") for r in rows]
    raw = meta["raw_keys"]
    dups = sorted({k for k in raw if raw.count(k) > 1})
    ambiguous = []
    for r in rows:
        seen: dict[tuple[Any, Any], int] = {}
        for f in findings(r):
            seen[(f.get("title"), f.get("file"))] = seen.get((f.get("title"), f.get("file")), 0) + 1
        ambiguous += [f"{r.get('key')}:{t}" for (t, _f), n in seen.items() if n > 1]
    expected = set(PHASE) if args.expect == "full" else ({k for k in args.expect.split(",") if k} if args.expect else None)
    missing = sorted(expected - set(keys)) if expected else []
    unexpected = sorted(set(keys) - expected) if expected else []
    print(f"rows          : {len(rows)} present | duplicate keys: {dups or 'none'} | ambiguous findings: {ambiguous or 'none'} | missing: {missing or 'none'} | unexpected: {unexpected or 'none'}")
    print("severity totals (raw):", tot)
    crit, high = unrefuted_ch(rows)
    print(f"unrefuted C/H : critical={crit} high={high} (refuted findings move to the false-positive section)")
    coverage_bad = bool(failed or dups or ambiguous or missing or unver)
    hint = "Do-not-merge" if crit else "Incomplete" if coverage_bad else "Merge-after-fixes" if high else "Merge"
    print(f"verdict hint  : {hint} (scripts/verdict.py computes the verdict, gates included)")
    return 0 if hint == "Merge" else 1


# ---- render -------------------------------------------------------------------
def _loc(f: dict[str, Any]) -> str:
    s = str(f.get("file", ""))
    if f.get("line"):
        s += f":{f['line']}"
    if f.get("line_end"):
        s += f"-{f['line_end']}"
    return s


def _keyed(path: str | None, field: str) -> dict[tuple[Any, Any, Any], str]:
    out: dict[tuple[Any, Any, Any], str] = {}
    if path:
        for e in json.loads(Path(path).read_text(encoding="utf-8")):
            out[(e["key"], e["file"], e.get("title"))] = str(e.get(field, ""))
    return out


def _akey(r: dict[str, Any], f: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (r.get("key"), f.get("file"), f.get("title"))


def _blocking(rows: list[dict[str, Any]], acc: dict[Any, str]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out = []
    for r in rows:
        if is_failed(r):
            continue
        ref = refuted_pairs(r)
        for f in findings(r):
            if f.get("severity") in ("critical", "high") and _akey(r, f) not in acc and (f.get("title"), f.get("file")) not in ref:
                out.append((r, f))
    return out


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _section_blocking(rows: list[dict[str, Any]], acc: dict[Any, str], trk: dict[Any, str], out: list[str]) -> None:
    out.append("## A. Confirmed Critical / High (not refuted, not accepted)\n")
    for n, (r, f) in enumerate(_blocking(rows, acc), 1):
        tag = "C" if f["severity"] == "critical" else "H"
        out.append(f"### {tag}{n}. {f.get('title', '')}\n")
        out.append(f"- Row: `{r['key']}` ({requested(r)}, ran_on={r.get('ran_on')}) — Phase {PHASE.get(r['key'], '?')} {f.get('item', '')}")
        out.append(f"- Location: `{_loc(f)}` | CWE: {f.get('cwe') or '-'} | Confidence: {f.get('confidence')}")
        if _akey(r, f) in trk:
            out.append(f"- Tracker: {trk[_akey(r, f)]}")
        out.append(f"- Issue: {f.get('issue')}")
        for k, label in (("trigger", "Trigger"), ("impact", "Impact")):
            if f.get(k):
                out.append(f"- {label}: {f[k]}")
        out.append(f"- Existing defense: {f.get('existing_defense') or 'none'}")
        if f.get("poc"):
            out.append(f"- PoC:\n```\n{str(f['poc']).rstrip()}\n```")
        if f.get("fix_plan"):
            out.append(f"- Fix plan: {f['fix_plan']}")
        if f.get("fix_diff"):
            out.append(f"- Fix diff:\n```diff\n{str(f['fix_diff']).rstrip()}\n```")
        if f.get("regression_test"):
            out.append(f"- Regression test:\n```\n{str(f['regression_test']).rstrip()}\n```")
        v = vmap(r).get((f.get("title"), f.get("file")))
        if v:
            out.append(f"- Skeptics: refuted={v.get('refuted')} votes={v.get('votes_returned')}/{v.get('votes_requested')} (refutes={v.get('refutes')}, below-tier={v.get('substituted_refutes', 0)}+{v.get('tier_mismatch_refutes', 0)}, off-commit={v.get('head_mismatch_refutes', 0)})")
            for line in v.get("reasonings", []):
                out.append(f"  - {line}")
        out.append("")


def _blocking_table(rows: list[dict[str, Any]], acc: dict[Any, str], trk: dict[Any, str], out: list[str]) -> None:
    out.append("## Blocking table (read by `full_sweep_report.py prev`)\n")
    out.append("| Tag | Severity | Row | Title | File | Tracker |")
    out.append("|---|---|---|---|---|---|")
    for n, (r, f) in enumerate(_blocking(rows, acc), 1):
        tag = ("C" if f["severity"] == "critical" else "H") + str(n)
        out.append(f"| {tag} | {f['severity']} | {r['key']} | {_cell(f.get('title', ''))} | {_cell(_loc(f))} | {_cell(trk.get(_akey(r, f), '-'))} |")
    out.append("")


def _section_accepted(rows: list[dict[str, Any]], acc: dict[Any, str], out: list[str]) -> None:
    out.append("## B. Accepted by the owner (rule 8), kept out of the blocking list\n")
    for r in rows:
        for f in findings(r):
            if _akey(r, f) in acc:
                out.append(f"### [{r['key']}] {str(f.get('severity', '')).upper()} — {f.get('title', '')}\n")
                out.append(f"- Location: `{_loc(f)}` | Confidence: {f.get('confidence')}")
                out.append(f"- Issue: {f.get('issue')}")
                out.append(f"- **Ruling**: {acc[_akey(r, f)]}")
                out.append("")


def _section_level(rows: list[dict[str, Any]], level: str, title: str, out: list[str]) -> None:
    out.append(title + "\n")
    for r in rows:
        fs = [f for f in findings(r) if f.get("severity") == level]
        if not fs:
            continue
        out.append(f"### [{r['key']}] — {len(fs)}\n")
        for f in fs:
            out.append(f"- **{f.get('title')}** — `{_loc(f)}` | {f.get('item', '')} | conf={f.get('confidence')}")
            out.append(f"  - Issue: {f.get('issue')}")
            if level == "medium":
                out.append(f"  - Existing defense: {f.get('existing_defense') or 'none'}")
                if f.get("fix_plan"):
                    out.append(f"  - Fix plan: {f['fix_plan']}")
        out.append("")


def _section_fp(rows: list[dict[str, Any]], out: list[str]) -> None:
    out.append("## E. False positives (refuted Critical/High and fp_candidates)\n")
    for r in rows:
        ref = refuted_pairs(r)
        refuted = [f for f in findings(r) if (f.get("title"), f.get("file")) in ref]
        if refuted:
            out.append(f"### [{r['key']}] refuted — {len(refuted)}\n")
            for f in refuted:
                out.append(f"- **{f.get('title')}** ({f.get('severity')}) — `{_loc(f)}`")
                for line in (vmap(r).get((f.get("title"), f.get("file"))) or {}).get("reasonings", []):
                    out.append(f"  - {line}")
            out.append("")
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        fps = rep.get("fp_candidates") or []
        if fps:
            out.append(f"### [{r['key']}] fp_candidates — {len(fps)}\n")
            out.append("| File:line | Pattern | Existing defense | Recommended |")
            out.append("|---|---|---|---|")
            for fp in fps:
                out.append("| " + " | ".join(_cell(fp.get(k, "")) for k in ("file_line", "pattern", "defense", "recommended")) + " |")
            out.append("")


def _section_unchecked(rows: list[dict[str, Any]], out: list[str]) -> None:
    out.append("## F. Unchecked, per row\n")
    for r in rows:
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        out.append(f"**{r['key']}** (files_scanned={rep.get('files_scanned')}{', FAILED' if is_failed(r) else ''})\n")
        for u in rep.get("unchecked") or []:
            out.append(f"- {u}")
        out.append("")


def _section_runrecord(rows: list[dict[str, Any]], d: dict[str, Any], meta: dict[str, Any], out: list[str]) -> None:
    out.append(f"## G. Run record (commit {d.get('head')})\n")
    out.append("| Row | Phase | Requested | Launched | ran_on | files | C | H | M | L | FP | unchecked | skeptic C+H / refuted |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    tot = dict.fromkeys(SEVS, 0)
    for r in rows:
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        c = sev_counts(r)
        for s in SEVS:
            tot[s] += c[s]
        vs = r.get("verdicts") or []
        ref = sum(1 for v in vs if isinstance(v, dict) and v.get("refuted") is True)
        out.append(f"| {r['key']} | {PHASE.get(r['key'], '?')} | {requested(r)} | {launched(r)} | {_cell(r.get('ran_on'))} | {rep.get('files_scanned')} | {c['critical']} | {c['high']} | {c['medium']} | {c['low']} | {len(rep.get('fp_candidates') or [])} | {len(rep.get('unchecked') or [])} | {len(vs)} / {ref} |")
    out.append(f"| **total** | | | | | | **{tot['critical']}** | **{tot['high']}** | **{tot['medium']}** | **{tot['low']}** | | | |")
    out.append("")
    out.append(f"failed[] = {effective_failed(rows)} / substituted[] = {d.get('substituted')} / fable_out = {d.get('fable_out')} / errors[] = {len(d.get('errors') or [])} / reruns = {meta.get('reruns')} / unverified = {sum(len(r.get('unverified') or []) for r in rows)}\n")


def _section_selfassess(rows: list[dict[str, Any]], acc: dict[Any, str], out: list[str]) -> None:
    ch = [f for _r, f in _blocking(rows, acc)]
    files = sum((r.get("report") or {}).get("files_scanned") or 0 for r in rows if isinstance(r.get("report"), dict))
    out.append("## H. Self-assessment\n")
    out.append("| Metric | Target | Actual |")
    out.append("|---|---|---|")
    out.append(f"| Files scanned (sum of each row's own count) | all | {files} |")
    out.append(f"| Confirmed Critical+High (accepted excluded) | — | {len(ch)} |")
    out.append(f"| PoC coverage (PoC-deferred included) | 100% | {sum(1 for f in ch if f.get('poc'))}/{len(ch)} |")
    out.append(f"| Fix-diff coverage | 100% | {sum(1 for f in ch if f.get('fix_diff'))}/{len(ch)} |")
    out.append(f"| Regression-test coverage | 100% | {sum(1 for f in ch if f.get('regression_test'))}/{len(ch)} |")
    out.append(f"| Rows failed | 0 | {len(effective_failed(rows))} |")
    out.append(f"| Unchecked items (sum over rows) | 0 | {sum(len((r.get('report') or {}).get('unchecked') or []) for r in rows if isinstance(r.get('report'), dict))} |")
    out.append("")


def _section_unchecked_summary(rows: list[dict[str, Any]], out: list[str]) -> None:
    # One parseable line per row: "- <key>: <text>" (prev reads exactly this shape).
    out.append("## Unchecked summary per row (the next sweep's {PREV_UNCHECKED})\n")
    for r in rows:
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        items = [str(u).replace("\n", " ").strip() for u in rep.get("unchecked") or []]
        text = "; ".join(items) if items else ("row FAILED — nothing read" if is_failed(r) else "none recorded")
        out.append(f"- {r['key']}: {text}")
    out.append("")


def cmd_render(args: argparse.Namespace) -> int:
    d, rows, meta = merged_rows(args.main, args.rerun)
    acc, trk = _keyed(args.accepted, "reason"), _keyed(args.tracker, "link")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    full = [f"# Full sweep appendices — generated from the result JSON (commit {d.get('head')})\n"]
    _section_blocking(rows, acc, trk, full)
    _section_accepted(rows, acc, full)
    _section_level(rows, "medium", "## C. Medium (not skeptic-verified — read ±30 lines before acting)", full)
    _section_level(rows, "low", "## D. Low", full)
    _section_fp(rows, full)
    _section_unchecked(rows, full)
    _section_runrecord(rows, d, meta, full)
    _section_selfassess(rows, acc, full)
    (out_dir / "appendices.md").write_text("\n".join(full), encoding="utf-8")
    for src in [args.main] + [s.partition("=")[2] for s in args.rerun]:
        try:
            shutil.copy(src, out_dir / Path(src).name)
        except (OSError, shutil.SameFileError) as e:
            print("warn: could not copy a result JSON:", e, file=sys.stderr)
    if args.summary:
        slim = [f"# Full sweep summary (commit {d.get('head')})\n"]
        _section_blocking(rows, acc, trk, slim)
        _blocking_table(rows, acc, trk, slim)
        _section_accepted(rows, acc, slim)
        _section_runrecord(rows, d, meta, slim)
        _section_selfassess(rows, acc, slim)
        _section_unchecked_summary(rows, slim)
        Path(args.summary).write_text("\n".join(slim), encoding="utf-8")
    print(f"wrote {out_dir}/appendices.md ({len(rows)} rows)" + (f" and summary {args.summary}" if args.summary else ""))
    return 0


# ---- prev ---------------------------------------------------------------------
_BLOCK_RE = re.compile(r"^\|\s*([CH]\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*((?:[^|\\]|\\.)+?)\s*\|\s*((?:[^|\\]|\\.)+?)\s*\|\s*((?:[^|\\]|\\.)+?)\s*\|")
_UNCHK_RE = re.compile(r"^-\s+([a-z][\w-]+):\s*(.*)$")


def cmd_prev(args: argparse.Namespace) -> int:
    text = Path(args.summary).read_text(encoding="utf-8")
    if args.appendices and Path(args.appendices).exists():
        text += "\n" + Path(args.appendices).read_text(encoding="utf-8")
    blocking, unchecked = [], {}
    in_unchecked = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_unchecked = line.startswith("## Unchecked summary per row")
        mb = _BLOCK_RE.match(line)
        if mb:
            blocking.append({"tag": mb.group(1), "severity": mb.group(2), "row": mb.group(3), "title": mb.group(4).replace("\\|", "|"), "file": mb.group(5).replace("\\|", "|"), "tracker": mb.group(6).replace("\\|", "|")})
        mu = _UNCHK_RE.match(line)
        if in_unchecked and mu:
            unchecked[mu.group(1)] = mu.group(2)
    print(json.dumps({"blocking": blocking, "unchecked_by_row": unchecked}, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("main")
    pc.add_argument("--rerun", action="append", default=[])
    pc.add_argument("--expect", help="'full' = the 19 full-sweep rows, or a comma list of row keys")
    pc.set_defaults(func=cmd_check)
    pr = sub.add_parser("render")
    pr.add_argument("main")
    pr.add_argument("--rerun", action="append", default=[])
    pr.add_argument("--accepted")
    pr.add_argument("--tracker")
    pr.add_argument("--out-dir", required=True)
    pr.add_argument("--summary")
    pr.set_defaults(func=cmd_render)
    pp = sub.add_parser("prev")
    pp.add_argument("summary")
    pp.add_argument("--appendices")
    pp.set_defaults(func=cmd_prev)
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
