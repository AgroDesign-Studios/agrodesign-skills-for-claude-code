# Optional integrations

The skill works on its own: a review, a computed verdict, a report. A project with a merge workflow can connect three things to it. Each is optional, and none may weaken the core: the read-only engine, one commit per review, the independent skeptics, and a verdict that never passes what did not run.

## 1. Delivery gates

Checks a merge needs beyond the code review: a changelog entry, a migration-number collision check, a regenerated API description, a test-change rule. Two kinds, both named in the project profile:

- **Mechanical gates** — a script the orchestrator runs before the verdict, printing one line per gate:

  ```
  GATE changelog-entry PASS
  GATE migration-number-unique FAIL 0042 is taken on the base branch
  GATE api-description-regenerated N-A no server change
  GATE test-change-rule UNMEASURED could not read the diff
  ```

  Save the output and pass it to `verdict.py --gates <file>`.

- **Judgment gates** — questions a reader answers (does the changelog line describe the change?). The `diff-bugs` brief lists each by its exact name and asks for a `gates` entry per name (`{"gate": "<name>", "result": "PASS|FAIL|N-A", "note": "..."}`); the engine's schema carries them.

Pass the expected names to `verdict.py`: mechanical ones to `--expect-mechanical` (read only from `--gates`), judgment ones to `--expect-judgment` (read only from the row `--gate-row` names, `diff-bugs` by default — a gate another row reports does not count, and a judgment result never stands in for a mechanical gate). Several reports of one gate combine to the worst. A missing gate → `Incomplete`; `UNMEASURED` → `Incomplete`; `FAIL` → an unresolved High. A finder that should have reported gates and returned none leaves them missing: re-run it.

## 2. Rounds and ledger (PR mode, after fixes)

A PR often takes several rounds: review, fix, review again. Without memory, round 2 re-raises what round 1 refuted, and the review never converges. Keep a small ledger per PR, outside the repository (or in a gitignored folder), written by the orchestrator after each round:

```json
{"pr": "123", "rounds": [{"round": 1, "head": "<sha>", "rows": ["diff-bugs", "diff-security"]}],
 "confirmed": [{"key": "diff-security", "file": "src/api/items.py", "title": "...", "severity": "high", "status": "open|fixed|invalidated", "round": 1}],
 "refuted": [{"key": "diff-bugs", "file": "src/util.py", "title": "...", "reason": "...", "round": 1}]}
```

Rules for round 2 and later:

- Review the **delta** since the last reviewed head (`git diff --name-only <last head>..HEAD`), with the whole PR's file list as read-only context; findings trace to the delta, or to a regression of a confirmed finding. A row runs when the delta's files trigger it, or when it owns a confirmed finding still `open`.
- Every brief lists the `open` confirmed findings (verify their closure) and the refuted ones **with their reasons** (an unsound reason is new evidence; absent new evidence, do not re-raise).
- **The ledger is advisory, never a veto.** No rule may dismiss a raised Critical/High without its skeptic pass: every mechanical "already refuted" filter ends up silencing some legitimate re-raise. The engine takes no ledger arguments on purpose.
- A finding is `fixed` only when a round has verified the fix, never because a fix commit exists. The merge waits while any confirmed finding is `open`.
- Freeze the tree while a round runs: an edit made mid-round is seen by some agents and not others.

## 3. Filing findings in a tracker

After the report — never inside the read-only fan-out — a project may file confirmed findings it will not fix in this change, and the Mediums worth keeping, as issues in its tracker. Name the command and the rule in the profile (who may file, labels, one issue per finding or grouped). Each issue cites file and line, never a secret, and links back to the review's record. `full_sweep_report.py render --tracker <file>` records the links beside each blocking finding, keyed by (row, file, exact title).

## 4. Rules from the base branch

A PR that edits the rubric, the profile or the engine would otherwise be judged by the rules it brings. To judge it by the base branch's rules, copy them out of the working tree before the round, into a folder outside the repository:

```bash
base=$(git rev-parse origin/main)
dir=$(mktemp -d)
git show "$base:.claude/review-profile.md" > "$dir/review-profile.md"
cp "<skill>/rubric/review-rubric.md" "$dir/review-rubric.md"   # or the base branch's copy, if the project vendors the skill
```

Then pass `rubric` and `profile` as those copies and `rules: {from: "origin/main", sha: "<base>"}` to the engine; every prompt names the copies and the result records them. If the project vendors the skill inside the repository, launch the base branch's copy of `agrodesign-deep-review.workflow.js` the same way.
