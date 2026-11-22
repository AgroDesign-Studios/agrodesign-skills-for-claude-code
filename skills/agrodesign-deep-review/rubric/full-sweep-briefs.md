# /agrodesign-deep-review full mode — finder brief templates (19 rows)

Fill-in-the-blank briefs for the full sweep, so composing 19 briefs is substitution, not writing. Each launched row's `brief` = **the shared preamble** (§A for a judgment row, §B for a transcription or quoting row) **+ that row's focus block from §C** + the project's own focus for that row, if its profile has one. Copy each row's `model`/`effort` **verbatim from the rubric's full table** (rule 6) — this file never restates tiers, so it cannot drift from the single source of truth.

The engine prepends the probe rules and the commit under review to every prompt and appends the tier report; the briefs do not repeat them.

## Placeholders

| Placeholder | Value |
|---|---|
| `{KEY}` | the row key, e.g. `a1-access-control` |
| `{ITEMS}` | the row's items, e.g. `item #1 (access control and privilege escalation)` |
| `{ITEM_HEADINGS}` | the rubric headings the finder reads, e.g. `### #1` |
| `{RUBRIC}` | absolute path of the rubric the engine was given |
| `{PROFILE}` | absolute path of the project profile, or `none — this project has no profile` |
| `{PROJECT}` | one paragraph from the profile: what the project is, its stack, how it is deployed and who can reach it (or `see the code; no profile`) |
| `{HEAD}` | the full commit id the frozen tree is at |
| `{DATE}` | that commit's date, `YYYY-MM-DD` |
| `{EXCLUDE}` | folders not to read: vendored, generated, build output, data (the rubric's defaults plus the profile's) |
| `{PREV}` | `none — first sweep`, or: `the previous sweep reviewed <commit> on <date>; its summary is <absolute path>` |
| `{PREV_UNCHECKED}` | the row's carry-forward from `full_sweep_report.py prev <previous summary>`: its unchecked line and its still-open blocking rows, or `none recorded` |

The carry-forward is what makes each sweep cheaper than the last: a finder told what its row left unread last time starts there, and a finder handed the prior findings marks each **carried** or **fixed** instead of re-discovering it.

---

## §A. Shared preamble — judgment rows (all rows except a10, a14a, a18)

```text
ROW {KEY} — FULL code review, whole-project scope, {ITEMS}.

The project: {PROJECT}

The tree you read: your working directory, frozen (detached) at {HEAD} ({DATE}). Read only: change nothing, run no git command that writes. Do not read: {EXCLUDE}.

The rubric is {RUBRIC}. Read its §2 rules 1, 2, 7, 8 and 13 and §3, then ONLY your own item section(s) ({ITEM_HEADINGS}). Do not read other phases. The project profile is {PROFILE}: read it whole (it is short) — its false-positive patterns, its trust model and its focus for {KEY}, if any, apply to you.

What came before: {PREV}. If there was a previous sweep, check every prior finding of your row against the CURRENT code: still present -> report it again with the title prefixed "[carried]"; fixed -> one fp_candidates row with pattern "fixed since previous sweep: <title>" and the fix as defense. Your row left these areas unchecked last time — cover them FIRST: {PREV_UNCHECKED}.

Evidence standard: every finding carries a one-shot reproducer or `PoC-deferred: <why>` plus the static evidence (rule 1); taint walked at least 3 hops upstream where rule 2 applies; Critical/High carry a unified fix_diff and a regression_test skeleton. Coverage first (rule 7): report every issue including uncertain or low-severity ones with an honest confidence; defended patterns go to fp_candidates with the defense. The scope is larger than one pass can read, so spend depth where the risk is and list in `unchecked` (with the reason) every area you skipped. Keep issue+impact+fix_plan under 120 words per finding. Set files_scanned. Severity per the rubric's §3.

YOUR FOCUS: <the row's focus block from §C, then the profile's focus for this row>
```

## §B. Shared preamble — transcription and quoting rows (a10, a14a, a18)

These rows transcribe tool output, observed configuration or two sides of a type, and must not speculate; their "what to run" and severity mapping live in the focus block.

```text
ROW {KEY} — FULL code review, {ITEMS}. Your role is <TRANSCRIPTION of tool output | TRANSCRIPTION of observed headers and configuration | VERBATIM TWO-SIDED QUOTING>, not judgment or cross-file speculation.

The tree: your working directory, frozen (detached) at {HEAD} ({DATE}). Read only: change nothing. The rubric is {RUBRIC}: read §2 rule 13 and your item section(s) only ({ITEM_HEADINGS}). The project profile is {PROFILE}.

What came before: {PREV}. For each prior finding of your row, say whether the current output still shows it: still present -> report again with the title prefixed "[carried]"; gone -> an fp_candidates row with pattern "fixed since previous sweep: <title>". Cover first what your row left unchecked last time: {PREV_UNCHECKED}.

Report hits verbatim with file:line (or the exact line and its source); do NOT speculate beyond the pattern list; uncertain = list under unchecked; a tool that is not installed, or a command that fails, goes under unchecked with its error line — never reported as clean. Set files_scanned.

YOUR FOCUS (what to run and the severity mapping): <the row's focus block from §C>
```

---

## §C. Per-row focus blocks

### a1-access-control (#1)
Enumerate every route or handler that takes an object id (path, query or body). For each, find the check that ties the object to the caller (owner, membership, tenant, role) — in the handler, a dependency, a decorator, middleware or the query's own filter. Report every handler where the check is absent, weaker than its sibling verbs' (DELETE checked, PUT not), done after the data was read, or done in the client. Admin-only operations: who can reach them, and through which alternative paths (bulk endpoints, exports, background-job triggers, file downloads by path). Sequential ids are an amplifier, not a finding on their own.

### a2-crypto-auth (#2, #4)
Secrets and keys: where each is read, what happens when it is missing (a fallback value is a finding), how it is compared (constant time), whether it reaches logs or errors. Authentication: token validation (algorithm pinned, signature, issuer, audience, expiry), refresh rotation and reuse detection under concurrency, logout and revocation, session cookie flags, password hashing and reset flows, brute-force limits, account enumeration through different errors or timings.

### a3-injection-ssrf-llm (#3, #5, #41)
Every place a query, command, template, path, regular expression or deserializer receives input: walk the taint 3 hops. Every outbound request: who controls the URL or host, redirect handling, connect-time address checks. AI/LLM code: prompt assembly from untrusted content, tools or actions the model can trigger, model output rendered or executed, data sent to providers, cost limits.

### a4-endpoint-upload (#6, #7)
Request models: fields a caller can set that they should not (mass assignment); size and page limits; rate limits on expensive or sensitive routes; error responses that leak internals; state changes on GET. Uploads and parsers: limits before reading, type by content, archive extraction paths and budgets, complex-format parsers on untrusted input, files served back with their stored type.

### a5-container-misconfig-integrity (#8, #37, #38)
Container and compose files, build context and ignore files, CI workflows: secrets baked or committed, root users, mutable image tags, published debug or admin ports, sensitive host mounts. Configuration defaults that are unsafe in production (debug, default credentials, exposed docs or consoles, wide CORS). Integrity: lockfile hashes, unsigned updates, `eval` of fetched code, CI steps that run untrusted input with secrets in scope.

### a6-null-safety (#9)
Server and client: lookups that may return nothing, empty collections reaching reductions or index access, optional fields assumed present, boundaries (off-by-one, overflow, float equality), dates with and without time zones mixed. Client code: API fields the server can omit or null, loading and error states rendered as if data were present.

### a7-async-concurrency (#10, #10.5)
Check-then-act on shared state (database rows, files, caches) without a lock, a conditional update or a unique constraint; transactions spanning network calls; blocking work inside an event loop; jobs that can run twice and are not idempotent; lock ordering; pools exhausted by one slow path; cancellation that leaves state half-written. External APIs: timeouts, bounded retries with backoff, partial failure, unvalidated responses, pagination loops.

### a8-logic-errors (#11, #12)
Business rules against their stated intent (names, docstrings, tests): inverted conditions, unreachable branches, state machines with missing transitions, wrong units, defaults that change behavior silently, copy-paste slips. Error handling: swallowed exceptions, errors reported as success, partial failure reported as complete, cleanup skipped on error paths.

### a9-db-perf-leaks (#13, #14, #15)
Queries in loops (N+1), unbounded result sets, filters and joins on columns without an index (read the schema), wide selects in hot paths. Resources not closed on every path, unbounded caches, temporary files left behind. Quadratic work over input-sized collections, work proportional to the whole dataset per request.

### a10-bundle-deps (#16, #27) — transcription
Run the stack's dependency scanners on the lockfiles that exist, in modes that install nothing and only read public advisory databases (for example `pip-audit --no-deps --disable-pip -r <requirements>`, `npm audit --package-lock-only --omit=dev --json`, `osv-scanner --lockfile <file>`; a tool that would install or build anyway is not run and goes under unchecked), and the bundler's size report if a build output is already present (never build). Transcribe each advisory: package, installed version, advisory id, severity as the tool gives it, fixed-in version. Severity mapping: tool critical/high on a runtime dependency → High; on a development-only dependency → Medium; others → Low. A scanner not installed → unchecked.

### a11-architecture (#17–#20)
Import cycles, modules reaching into another module's private parts, layering breaks (handlers holding business rules or raw queries, persistence calling the web layer), side effects at import time, API inconsistency (naming, status codes, error shapes, pagination), deprecated framework or library APIs and the version that removes them.

### a12-readability-refactor (#21–#26)
Misleading names, comments that contradict code, functions too long or doing several things, flag arguments, duplicated logic that must change together, hand-rolled mechanisms the framework provides, legacy idioms the newer code has left behind. Severity Low or Medium by nature; group repeated instances into one finding with a count (rule 9: confirm the count by a project-wide search).

### a13-reuse-ops (#28–#31)
Helpers re-implemented in several places; critical paths (authorization, money, deletion, untrusted parsing) without tests, and tests that assert nothing (read them, never run them); hard-coded hosts, ports, paths, limits; configuration read in many places or never validated; deployment, rollback, secret rotation and backup steps missing or out of date in the docs.

### a14a-headers-tls (#32, #35) — transcription
Read the response-header configuration where it is set (the application's middleware, the reverse proxy's configuration) and transcribe each security header and its value: Content-Security-Policy, Strict-Transport-Security, X-Content-Type-Options, frame protection, Referrer-Policy, Permissions-Policy. TLS: protocol and cipher settings in configuration; outbound calls that disable certificate checks (search for the stack's switch, e.g. `verify=False`, `rejectUnauthorized: false`, `InsecureSkipVerify`). Only if the profile names a deployment URL to probe, one read-only `curl -sI` against it. Severity mapping: a missing CSP or HSTS on an authenticated web app → Medium; disabled certificate checks on outbound calls → High; others → Low.

### a14b-storage-protocol (#33, #34)
Client storage of tokens or personal data, data left after logout, `postMessage` handlers without an origin check, service workers caching authenticated responses. Proxy ↔ application: request smuggling (conflicting length headers, different parsers), cache keys that miss headers the response varies on, CORS reflecting any origin with credentials, trust in the Host header or forwarded headers.

### a15-threat-model (#36)
Pick the project's most valuable assets and its most exposed entry points (from the profile and the routes). For each, narrate the worst a realistic attacker (an outsider, an ordinary user, a compromised integration) can do and what stops them; a missing stop is a finding. Business-action abuse: limits, replay, ordering, trust in client-side values.

### a16-migration-deploy (#39, #40)
Migrations: locking or rewriting large tables, dropping data without a path back, irreversible steps without a note, ordering or numbering collisions, the migration chain and the schema snapshot or ORM models disagreeing. Deploy manifests: resource limits, probes, non-root users, read-only file systems, secrets in plain manifests, privileged or host-network workloads, mutable image tags, rollout strategy that can take everything down at once.

### a17-a11y-i18n (#42, #43)
Interactive elements without accessible names, keyboard traps, dialogs that do not manage focus, missing form labels, images without alternatives, colour contrast in the theme. Internationalization: hard-coded user-facing strings, keys missing from one language file, dates, numbers and plurals formatted by hand. Group repeats into one finding with a count.

### a18-type-drift (#44) — two-sided quoting
Find the server's API description (an OpenAPI file, schema classes, route models) and the client's hand-written types for the same payloads. For each payload the client uses, quote both sides verbatim and report every field present on one side only, every type or nullability difference, and every enum value out of sync. No judgment beyond the comparison; a payload whose server side you cannot find goes under unchecked. Severity mapping: a required field missing on the client, or a type mismatch → Medium; an optional field or enum drift → Low.
