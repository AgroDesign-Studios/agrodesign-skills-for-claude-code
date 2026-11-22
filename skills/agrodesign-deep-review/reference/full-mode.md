# Full mode — setup, briefs and the record

A full sweep reviews the whole project against all 44 items with 19 rows. It is a recurring product: each sweep starts from the last one's record, so it gets cheaper and more complete over time.

## Setup (before composing briefs)

1. **Freeze a tree.** Make a worktree detached at the commit to review, and review there — never in a working tree someone is editing (agents read files, not a git ref, so an edit mid-run gives a review of a tree that never existed):

   ```bash
   git fetch origin
   git worktree add --detach <path outside the repository> origin/main
   ```

   Record its full commit id and date: they are `head` for the engine and `{HEAD}` / `{DATE}` in the briefs. Run the sweep from a Claude Code session whose working directory is that worktree (start `claude` there): agents read their working directory, and the engine's head check fails every row that read another tree.

2. **Load the previous sweep.** If a previous summary exists (wherever the project keeps it — see Record below), run:

   ```bash
   python3 -I <skill>/scripts/full_sweep_report.py prev <previous summary.md>
   ```

   and fold each row's blocking rows and unchecked line into that row's `{PREV_UNCHECKED}`; `{PREV}` names the previous commit, date and summary path. The first sweep uses `none — first sweep` and `none recorded`.

## Briefs

Compose each brief from `rubric/full-sweep-briefs.md`: the shared preamble (§A judgment rows, §B transcription rows) with its placeholders filled + the row's §C focus block + the profile's focus for that row. The 19 rows and their model/effort are the rubric's full table, copied verbatim. Do not hand-write 19 briefs.

## After the run

1. **Bookkeeping first.**

   ```bash
   python3 -I <skill>/scripts/full_sweep_report.py check <result.json> --expect full [--rerun KEY=FILE ...]
   ```

   Read `failed[]`, `substituted[]`, `errors[]`, each row's `ran_on` and the unverified count before the findings. Re-run each failed row alone, on the same commit, at its own tier (a fresh Workflow call with only that row), and pass the new result as `--rerun KEY=FILE`.

2. **Verdict.** `scripts/verdict.py <result.json> --head <commit> --expect-rows <the 19 keys> [--rerun KEY=FILE ...]` (the same re-run files as above; a re-run on another commit is refused). A full sweep's verdict is a health signal, not a merge decision.

3. **Record.**

   ```bash
   python3 -I <skill>/scripts/full_sweep_report.py render <result.json> [--rerun KEY=FILE ...] \
     [--accepted <accepted.json>] [--tracker <tracker.json>] \
     --out-dir <logs folder>/<DATE>/ --summary <summary.md>
   ```

   `appendices.md` holds everything (Medium, Low, false positives, every unchecked item) and belongs in a logs folder; the summary holds what the next sweep needs (blocking findings with fix diffs, the blocking table, accepted risks, the run record, the self-assessment, the per-row unchecked line). Keep the summary where the next sweep can find it — committed beside the project's other review records, if the project keeps them in the repository. `--accepted` and `--tracker` take the owner's rule-8 acceptances and the tracker links, keyed by (row, file, exact title).

4. **Report** with the rubric's §16 final summary: severity rollup, priority list, false-positive list, roadmap, out-of-scope observations, self-assessment (with every substituted tier named).

## Traps

- A row with a terse brief can read one module and return a near-empty result that looks clean. Read its `unchecked` and `files_scanned`: a legitimate empty result and an under-read are told apart only there.
- Transcription rows (a10, a14a) depend on installed tools; a missing tool is an unchecked item, never a clean row.
- Long top-tier rows are the long pole of a sweep. Do not start fixing while it runs.
