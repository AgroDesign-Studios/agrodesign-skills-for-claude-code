# AgroDesign skills for Claude Code

AgroDesign Studios が公開している、[Claude Code](https://claude.com/claude-code) 用の汎用スキルです。アプリを GitHub で公開する準備に使います。1 つは、公開しようとしている中身そのものをセキュリティの観点で検査し、そのあとの送信（push）・公開への切り替え・リリースを、それぞれオーナーの承認の後にだけ行うスキルです。もう 1 つは、実行されなかった項目を決して合格扱いにしない判定を出す、網羅的なコードレビューのスキルです。

*English follows below.*

| スキル | 用途 | 呼び出し方 |
|---|---|---|
| [`agrodesign-github-publish`](skills/agrodesign-github-publish/SKILL.md) | GitHub での公開（公開リポジトリへの push、リリースのファイル・タイトル・本文、ソースの抜粋、新しい非公開リポジトリの公開への切り替え）。まず機械的な検査をし、次に Claude・Codex・Grok が順に、送る中身そのものを読みます。最後に Claude がすべての指摘を確かめ、結果を中身の digest に結び付けて封印（seal）します。そこで承認 1 を待って止まり、検査したコミットだけを push します。ここで終えることもできます。リポジトリを公開にするときは、切り替えで見えるようになるものをすべて読み、承認 2 を待って止まってから切り替え、リリースが承認されていればそれを作り、切り替えとリリースの両方をログインなしで確かめます。 | `/agrodesign-github-publish <公開するもの>` |
| [`agrodesign-deep-review`](skills/agrodesign-deep-review/SKILL.md) | 徹底したコードレビュー。PR モード（プルリクエストかブランチの差分）と、全体モード（プロジェクト全体を 11 段階・44 項目）があります。読み取りだけの検出役は、1 つのコミットと、役ごとに決めたモデルとエフォートに固定します。Critical/High の指摘にはすべて独立した検証役が付き、判定は計算で出します。実行されなかった行は `Incomplete` で、合格にはなりません。 | `/agrodesign-deep-review <PR 番号かブランチ>` · `/agrodesign-deep-review --full` |

これらは独立したスキルです。`agrodesign-deep-review` は Claude Code に組み込まれた `/code-review` コマンドとは別のもので、スキルが呼び出すツールの提供元である Anthropic・OpenAI・xAI と提携しておらず、その公認を受けたものでもありません。

## 必要なもの

| ツール | 使うスキル | 無いとき |
|---|---|---|
| Claude Code（Agent ツールの `Plan` サブエージェントが使えること） | 両方 | — |
| Claude Code の **Workflow** ツール（複数エージェントのワークフローのスクリプト） | `agrodesign-deep-review` | スキルは止まり、判定を出しません。手で並べて実行しても、すべての指示にコミットと探査の規則を固定し、Critical/High をすべて適切な層の検証役で確かめ、判定が読む記録を返す仕組みがないためです |
| Claude のモデル層 `fable`・`opus`・`sonnet`・`haiku` を使えること | `agrodesign-deep-review` | 最上位（`fable`）のエージェントを起動できないときは、同じエフォートの `opus` で 1 回だけやり直し、置き換えたと報告します。必要な層に届かない検証役は、指摘を認める（確定する）ことはできても、退けることはできないので、指摘は残ります（レビューは甘くならず、厳しくなります） |
| Python 3.11 以降（3.14 なら zstd のアーカイブも読めます） | 両方のスキルのスクリプト | — |
| git | 両方 | — |
| [gitleaks](https://github.com/gitleaks/gitleaks) | `agrodesign-github-publish` の Step 1 | `scan` は結果を出さずに exit 3 で終わり、検査は進められません（免除はありません） |
| [Codex CLI](https://github.com/openai/codex) 0.156 以降（permission profile を使います）、ログイン済み | `agrodesign-github-publish` の Step 3 | Codex の段を実行できません。その段が実行されるか、オーナーがその 1 回の公開に限って言葉で免除し、判定に記録されるまで、公開は保留されます |
| [grok CLI](https://x.ai/cli)、ログイン済み（空のフォルダからログイン） | `agrodesign-github-publish` の Step 4 | Grok の段を実行できません。上と同じく、保留か免除です |
| Linux と coreutils の `timeout` | `agrodesign-github-publish` の Codex 用の補助スクリプト（プロセスの封じ込めに `prctl` を使います）と Grok 用の補助スクリプト | そのマシンではこれらの段を実行できません（上と同じく、保留か免除） |
| [GitHub CLI](https://cli.github.com/) `gh`（送り先に権限のあるアカウントでログイン済み） | `agrodesign-github-publish` の Step 7 以降（承認はリポジトリの ID に結び付き、事前点検はリポジトリを読み、公開への切り替えとリリースは `gh` で行います）と `--repo-meta`、`agrodesign-deep-review` の PR モードで番号を指定するとき | push・切り替え・リリースは実行できません（手作業では決して行いません）。レビューでは、ブランチを自分でチェックアウトしてください |
| Node.js | レビューエンジンのテストだけ | そのテストはスキップされます |

執筆時点で固定しているモデル（Claude の段は Claude Opus 5.5、Codex は `gpt-6-astra` の `ultra`、Grok は `grok-4.7`）は、それぞれ 1 か所で決めています。Claude はスキルの表と `github_upload_check.py` の `CLAUDE_STAGE_MODEL`（Claude の段の `ran_on` に出てくるべきモデルの系統）の組、Codex は `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`、Grok は `GROK_REVIEW_MODEL` です。スクリプトは、頼んだモデルが実際に答えたことを確かめます。

## 入れ方

このリポジトリを clone し、各スキルのフォルダを、Claude Code が読むスキルのフォルダにコピー（またはリンク）します。

```bash
git clone https://github.com/AgroDesign-Studios/agrodesign-skills-for-claude-code.git
mkdir -p ~/.claude/skills
cp -R agrodesign-skills-for-claude-code/skills/agrodesign-github-publish ~/.claude/skills/      # このアカウントのすべてのプロジェクト用
cp -R agrodesign-skills-for-claude-code/skills/agrodesign-deep-review  <your repo>/.claude/skills/   # または 1 つのプロジェクト用
```

そのあと:

- **agrodesign-github-publish** — 自分の非公開の値（ホスト名、非公開のアドレス、アカウント名、社員の名前とメールアドレス、サーバーのパス、社内のプロジェクト名）を、どの git の作業フォルダにも入らない `~/.config/agrodesign-github-publish/values.json` に書きます（[形式と決まり](skills/agrodesign-github-publish/reference/values-file.md)、英語）。この値が無いと、封印（seal）の段は実行を拒みます。
- **agrodesign-deep-review** — 必要なら、レビューするリポジトリに `.claude/review-profile.md` としてプロジェクトの設定を置きます。技術構成、デプロイと信頼のモデル、どのパスがどのレビュー行の対象になるか、そのプロジェクトに特有の誤検知の型と探査の規則です（[ひな形](skills/agrodesign-deep-review/reference/project-profile.md)、英語）。評価基準（rubric）そのものは、特定の技術構成に依存しません。

入れるものは固定してください。`main` からではなくリリースのタグからコピーし、それを記録します（次の節）。

## バージョン

リリースには `vMAJOR.MINOR.PATCH`（セマンティック バージョニング）のタグを付けます。MAJOR リリースはスキルが保証することや読むファイル（values ファイルの形式、エンジンの引数）を変え、MINOR リリースは機能を足し、PATCH リリースは直します。各リリースの本文に、スキルごとの変更を書きます。

これらのスキルで準備したアプリには、使ったスキルとそのバージョンを記録します。たとえば README に:

```markdown
## このリリースの検査方法
AgroDesign skills for Claude Code（`agrodesign-github-publish` と `agrodesign-deep-review`）の
AgroDesign-Studios/agrodesign-skills-for-claude-code <リリースのタグ>（commit <コミット ID の全桁>）で準備しました。
```

このリポジトリの名前は、v0.1.1 で変えるまで `claude-code-skills` でした。旧名を新しいリポジトリが使わない限り、GitHub は旧アドレスをこのリポジトリへ転送します。旧名と v0.1.0（またはそのコミット）を書いた記録も、同じコードを指したままです。

## テストの実行

テストは架空の値だけを使います（`gitleaks`・`grok`・`codex`・`gh` の代わりのスタブ、送り先としての手元の bare リポジトリ、ログインなしの API の代わりの手元の HTTP サーバー）。どこにも何も送りません。

```bash
cd agrodesign-skills-for-claude-code
python3 -m unittest discover -s tests -v
```

レビューエンジンのテストには Node.js が、Codex 用の補助スクリプトのテストには Linux と coreutils の `timeout` が要ります。それらが無いマシンでは、そのテストはスキップと表示されます。スキップされたテストは、そのマシンでは合格していません。

## 公開への切り替えが扱う範囲

リポジトリを公開にすると、履歴のすべて、すべてのブランチとタグ、issue、プルリクエスト、リリース、Actions のログ、設定が一度に見えるようになります。`agrodesign-github-publish` が切り替えるのは、新しく空の非公開リポジトリとして作り、このスキルが封印した push（`--whole-history` と `--repo-meta` を付けて stage したもの）以外は何も受け取っていないものだけです。切り替える前に、すべての ref、GitHub の活動記録（force push や削除されたブランチが無いこと。それらのコミットは取得できるまま残るため）、issue、プルリクエスト、リリース、タグ、ラベル、Pages、wiki（wiki 自体の git リポジトリも）、Actions の記録を読み、検査していないものが何も無かったときにだけ切り替えます（GitHub Projects のプロジェクトは、リポジトリとは別に公開・非公開が決まるので読みません）。切り替えた直後にもう一度すべてを読み、その間に何かが増えていれば、非公開へ戻します。ほかのリポジトリ（issue や wiki がある古いもの、ほかから push を受けたもの）は断ります。新しいリポジトリを作り、検査した履歴をそこへ push してください。非公開に戻しても、公開中に作られた複製やフォークは回収できません。

## ライセンス

[MIT](LICENSE) © 2026 AgroDesign Studios。ここに名前を挙げた Codex・Grok・gitleaks・Claude Code などのツールは、それぞれの所有者の製品で、このリポジトリには含まれていません。

---

# AgroDesign skills for Claude Code (English)

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
git clone https://github.com/AgroDesign-Studios/agrodesign-skills-for-claude-code.git
mkdir -p ~/.claude/skills
cp -R agrodesign-skills-for-claude-code/skills/agrodesign-github-publish ~/.claude/skills/      # for every project of this account
cp -R agrodesign-skills-for-claude-code/skills/agrodesign-deep-review  <your repo>/.claude/skills/   # or for one project
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
Prepared with AgroDesign skills for Claude Code (`agrodesign-github-publish` and `agrodesign-deep-review`)
from AgroDesign-Studios/agrodesign-skills-for-claude-code <release tag> (commit <full commit id>).
```

This repository was named `claude-code-skills` until v0.1.1, which renamed it. GitHub forwards the old address to this one as long as no new repository takes the old name, and a record that names the old repository with v0.1.0 or its commit still identifies the same code.

## Run the tests

The tests use made-up values only (stub `gitleaks`, `grok`, `codex` and `gh`, a local bare repository as the push destination, a local HTTP server as the anonymous API); nothing is sent anywhere.

```bash
cd agrodesign-skills-for-claude-code
python3 -m unittest discover -s tests -v
```

The review-engine tests need Node.js, and the Codex-helper tests need Linux and coreutils `timeout`; on a machine without them those tests are reported as skipped — a skipped test has not passed there.

## What the switch to public covers

Making a repository public exposes its whole history, every branch and tag, issues, pull requests, releases, Actions logs and settings at once. `agrodesign-github-publish` switches only a repository made new, empty and private that has received nothing but pushes it sealed (staged with `--whole-history` and `--repo-meta`), after reading every ref, GitHub's activity log (no force pushes or deleted branches, whose commits stay fetchable), issue, pull request, release, tag, label, Pages site, wiki (and the wiki's own git repository) and Actions record and finding nothing unchecked (a project in GitHub Projects keeps its own visibility and is not read); it reads them all again right after the switch and switches back to private if anything arrived in between. Any other repository — an old one with issues, a wiki, or pushes from elsewhere — is refused: make a new one and push the checked history to it. Switching back to private does not recall copies or forks made while it was public.

## Licence

[MIT](LICENSE) © 2026 AgroDesign Studios. Codex, Grok, gitleaks, Claude Code and the other tools named here are their owners' products and are not included.
