# Claude Code skills

General-purpose [Claude Code](https://claude.com/claude-code) skills from AgroDesign Studios, used to prepare applications for publication on GitHub: a security check of exactly what is about to be published followed by the push, the switch to public and the release, each only after the owner's approval; and a comprehensive code review with a verdict that never passes what did not run.

| Skill | Use it for | Invoke |
|---|---|---|
| [`agrodesign-github-publish`](skills/agrodesign-github-publish/SKILL.md) | Publishing on GitHub (a push to a public repository, a release's files, title and notes, a source extract, or making a new private repository public): a mechanical scan, then Claude, Codex and Grok each read the exact set in turn, a final Claude pass verifies every finding, and the result is sealed to the set's digest. It then stops for approval 1 and pushes only the checked commit; it can end there. To make the repository public it reads everything the switch would expose, stops for approval 2, then switches, creates the release if one was approved, and checks both without logging in. | `/agrodesign-github-publish <what to publish>` |
| [`agrodesign-deep-review`](skills/agrodesign-deep-review/SKILL.md) | A thorough code review: PR mode (a pull request or branch, diff-scoped) or full mode (the whole project, 44 items in 11 phases). Read-only finders pinned to one commit and to a model and effort each, independent skeptics on every Critical/High, and a computed verdict where a row that did not run is `Incomplete`, never a pass. | `/agrodesign-deep-review <PR number or branch>` · `/agrodesign-deep-review --full` |

These are independent skills: `agrodesign-deep-review` is not Claude Code's built-in `/code-review` command and is not affiliated with Anthropic, OpenAI or xAI, whose tools the skills call.

## Requirements

| Tool | Needed by | Without it |
|---|---|---|
| Claude Code, with the Agent tool's `Plan` subagent | both skills | — |
| Claude Code's **Workflow** tool (multi-agent workflow scripts) | `agrodesign-deep-review` | the skill stops and gives no verdict: a hand-run fan-out has no engine to pin the commit and the probe rules into every prompt, verify every Critical/High with skeptics at tier, or return the bookkeeping the verdict reads |
| Access to the Claude model tiers `fable`, `opus`, `sonnet`, `haiku` | `agrodesign-deep-review` | a top-tier (`fable`) agent that cannot start is re-run once on `opus` at the same effort and reported as substituted; a skeptic below its required tier can confirm a finding but never refute it, so findings stand (the review gets stricter, not looser) |
| Python 3.11 or later (3.14 also reads zstd archives) | both skills' scripts | — |
| git | both | — |
| [gitleaks](https://github.com/gitleaks/gitleaks) | `agrodesign-github-publish` Step 1 | `scan` exits 3 with no result; the check cannot proceed (no waiver) |
| [Codex CLI](https://github.com/openai/codex) 0.156 or later (permission profiles), logged in | `agrodesign-github-publish` Step 3 | the Codex stage cannot run; the upload is held until it runs or the owner waives that stage for that one upload, in words recorded in the verdict |
| [grok CLI](https://x.ai/cli), logged in (from an empty folder) | `agrodesign-github-publish` Step 4 | the Grok stage cannot run; held or waived as above |
| Linux, with coreutils `timeout` | `agrodesign-github-publish`'s Codex helper (process containment uses `prctl`) and Grok helper | those stages cannot run on that machine (held or waived as above) |
| [GitHub CLI](https://cli.github.com/) `gh`, logged in with access to the destination | `agrodesign-github-publish` from Step 7 (approvals are bound to the repository's id, the preflight reads the repository, the switch and the release go through it), and `--repo-meta`; `agrodesign-deep-review` PR mode by number | the push, the switch and the release cannot run (they are never done by hand); for the review, check out the branch yourself |
| Node.js | only the tests of the review engine | those tests are skipped |

The model pins at the time of writing — Claude Opus 5.5 for the Claude stages, Codex `gpt-6-astra` at `ultra`, Grok `grok-4.7` — are set in one place each: the skill's table with `CLAUDE_STAGE_MODEL` in `github_upload_check.py` (the family the Claude stages' `ran_on` must name), `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`, and `GROK_REVIEW_MODEL`. The scripts check that the model asked for is the model that answered.

## Install

Clone this repository and copy (or link) each skill folder into a skills folder Claude Code reads:

```bash
git clone https://github.com/AgroDesign-Studios/claude-code-skills.git
mkdir -p ~/.claude/skills
cp -R claude-code-skills/skills/agrodesign-github-publish ~/.claude/skills/      # for every project of this account
cp -R claude-code-skills/skills/agrodesign-deep-review  <your repo>/.claude/skills/   # or for one project
```

Then:

- **agrodesign-github-publish** — write your private values (host names, private addresses, account names, staff names and e-mail addresses, server paths, internal project names) to `~/.config/agrodesign-github-publish/values.json`, outside every git checkout: [format and rules](skills/agrodesign-github-publish/reference/values-file.md). The seal refuses a run without them.
- **agrodesign-deep-review** — optionally add a project profile, `.claude/review-profile.md` in the reviewed repository: your stack, deployment and trust model, which paths trigger which review rows, your own false-positive patterns and probe rules ([template](skills/agrodesign-deep-review/reference/project-profile.md)). The rubric itself is stack-neutral.

Pin what you install: copy from a release tag, not from `main`, and record it (below).

## Versions

Releases are tagged `vMAJOR.MINOR.PATCH` (semantic versioning): a MAJOR release changes what a skill guarantees or the files it reads (a values-file format, an engine argument); a MINOR release adds; a PATCH release fixes. Each release's notes list what changed per skill.

An application prepared with these skills records which ones it used and their version, for example in its README:

```markdown
## How this release was checked
Prepared with the Claude Code skills `agrodesign-github-publish` and `agrodesign-deep-review` from
AgroDesign-Studios/claude-code-skills v0.1.0 (commit <full commit id>).
```

## Run the tests

The tests use made-up values only (stub `gitleaks`, `grok`, `codex` and `gh`, a local bare repository as the push destination, a local HTTP server as the anonymous API); nothing is sent anywhere.

```bash
cd claude-code-skills
python3 -m unittest discover -s tests -v
```

The review-engine tests need Node.js, and the Codex-helper tests need Linux and coreutils `timeout`; on a machine without them those tests are reported as skipped — a skipped test has not passed there.

## What the switch to public covers

Making a repository public exposes its whole history, every branch and tag, issues, pull requests, releases, Actions logs and settings at once. `agrodesign-github-publish` switches only a repository made new, empty and private that has received nothing but pushes it sealed (staged with `--whole-history` and `--repo-meta`), after reading every ref, GitHub's activity log (no force pushes or deleted branches, whose commits stay fetchable), issue, pull request, release, tag, label, Pages site, wiki (and the wiki's own git repository) and Actions record and finding nothing unchecked; it reads them all again right after the switch and switches back to private if anything arrived in between. Any other repository — an old one with issues, a wiki, or pushes from elsewhere — is refused: make a new one and push the checked history to it. Switching back to private does not recall copies or forks made while it was public.

## Licence

[MIT](LICENSE) © 2026 AgroDesign Studios. Codex, Grok, gitleaks, Claude Code and the other tools named here are their owners' products and are not included.
