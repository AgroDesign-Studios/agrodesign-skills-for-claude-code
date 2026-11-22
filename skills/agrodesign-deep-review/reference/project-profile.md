# Project profile — template

Copy this file to `.claude/review-profile.md` in the repository you review, fill in what applies and delete what does not. Every `/agrodesign-deep-review` agent reads it whole, so keep it short (one or two screens). It is committed with the code: write nothing secret or private in it — no host names, addresses, accounts or credentials (the review needs the *shape* of your deployment, not its values).

```markdown
# Review profile — <project name>

## Project
<One paragraph: what the project does, its stack (languages, frameworks, database), how it is
deployed, and who can reach it (public internet / company network / single tenant / invitation
only). This paragraph becomes {PROJECT} in the full-sweep briefs.>

## Trust model
- Users: <roles and what each may do>
- Deliberate decisions a reviewer could mistake for bugs, and where each is recorded:
  - <e.g. "every signed-in user may read every report — decision recorded in docs/decisions/0007.md">

## PR trigger paths (rubric rule 6)
| Trigger class | Paths |
|---|---|
| server-side request handling | <e.g. src/api/**, src/services/**, src/auth/**> |
| concurrency | <e.g. src/jobs/**, src/workers/**> |
| database | <e.g. migrations/**, src/models/**> |
| client UI | <e.g. web/src/**> |
| API schema + client types | <e.g. openapi.json + web/src/types/**> |
| container/build/deploy/CI | <e.g. Dockerfile, compose*.yml, deploy/**, .github/workflows/**> |
| dependencies | <e.g. requirements*.txt, package.json, *.lock> |
| AI/LLM integration | <e.g. src/llm/**> |
| hot paths | <e.g. src/search/**> |

## Do not read
<vendored, generated or data folders beyond the rubric's defaults>

## False-positive patterns (added to rubric rule 8)
| Pattern | Why flagged | Why it is safe here |
|---|---|---|

## Probe rules (added to the engine's; they only add restrictions)
- <e.g. "Treat every database URL in the environment as production.">
- <e.g. "Never run `make` — it deploys.">

## Delivery gates (optional; reference/integrations.md)
- Mechanical: `<command that prints GATE <name> PASS|FAIL|N-A|UNMEASURED lines>` — expected: <names>
- Judgment (reported by diff-bugs): <gate-name>: <what PASS means>

## Items that do not apply
- <e.g. "#32, #33: no browser client">

## Focus per row (optional)
- a1-access-control: <where object ownership lives in this codebase>
```
