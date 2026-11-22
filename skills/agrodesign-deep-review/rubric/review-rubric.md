# Code review rubric (generic, v1)

The rubric every `/agrodesign-deep-review` agent reads: execution rules, severity, the output format, and 44 review items in 11 phases (A–K). It is stack-neutral on purpose. What only your project knows — its stack, how it is deployed, its auth model, which paths trigger which PR rows, its own false-positive patterns, its delivery gates, its extra probe rules — goes in the project profile (§1), never in this file.

## Read map

- The orchestrating session reads §1–§4 and §16 only.
- Each review agent reads §2 rule 8 (false positives), §3 and the `### #N` sections of its own items — never all eleven phases.
- The project profile, when there is one, is read by everyone in full: it is short by design.

---

## 1. Project profile

A project adds its facts in a profile file, by default `.claude/review-profile.md` in the repository under review (template: `reference/project-profile.md` beside the skill). Every brief names it; a review without one runs on this rubric alone and says so in its report. The profile may:

- describe the stack, the deployment context and the trust model (who the users are, what is reachable from where);
- map each PR trigger class of rule 6 to path patterns of the project;
- add false-positive patterns to rule 8's table, each with why it is safe *here*;
- add probe rules (they only add restrictions to rule 13; they never remove one);
- name delivery gates (rule 6, *Delivery gates*) and where their results come from;
- name items that do not apply (an API with no browser client has no #32) — a skipped item is reported as skipped, never as clean.

A profile holds no secret and no value that must stay private: it is committed with the code.

---

## 2. Execution rules

1. **PoC mandatory** — every vulnerability report carries a one-shot reproducer (an HTTP request, a test, a browser console line). A verbal-only item is a false-positive candidate. **A reproducer is run inside rule 13 or not at all**: one that cannot be is reported as `PoC-deferred: <why>` with the static evidence that stands in for it, which is a legal `poc` value and never a reason to drop or downgrade the finding.
2. **Taint tracking mandatory** — for string-built SQL, `eval`, dynamic import, a shell command built from input, a template rendered from input, or a path joined from input, walk the value upstream **at least 3 hops** and confirm what bounds it: a schema-validated field, an allow-list constant, a value from a trusted table. If nothing bounds it → Critical/High.
3. **Fix diff + regression test** — each Critical/High finding carries a unified diff that applies, a test skeleton with its assert lines, and a side-effect rating (Low/Medium/High + scope).
4. **Run modes** (the two modes check DIFFERENT content):
   - **PR mode** (a pull request or a branch against its base): a pre-merge gate. Conditional diff-scoped rows (rule 6 PR table) plus the project's delivery gates. Whole-repository phases (E, F, G, H, K sweeps, #16, #27–#35, #42–#44) are skipped **by design** — the full sweep owns them; do not re-add them to a PR review.
   - **Full mode**: all phases A–K over the whole project, 19 rows (rule 6 full table), on a frozen tree.
   - Modifiers: `--sequential` (rows one after another) / `--review-only` (the default: report only) / `--auto-fix` (fixes after the user approves the fix plan).
5. **Scope** — PR mode: the files the diff changes (`git diff --name-only <base>...HEAD`); agents may read other files to trace a value, but every finding traces to the diff. Full mode: the whole repository minus vendored, generated and build output (`node_modules/`, `.venv/`, `vendor/`, `dist/`, `build/`, `__pycache__/` and what the profile adds).
6. **Agent split + model/effort tiers** — the fan-out runs through the Workflow engine (`agrodesign-deep-review.workflow.js`), which pins **model AND effort per agent**, runs every review agent read-only (`agentType: 'Explore'`), leads every prompt with the probe rules and the commit under review, re-runs a top-tier agent that returns nothing once on opus at the same effort, and has every agent report the model its system prompt names (`ran_on`) and the HEAD it saw (`head_seen`). The tables below are the **single source of truth** for tiers — the orchestrator copies each row's Model/Effort into the workflow args verbatim; never omit them to "inherit the session's model". Four tiers, default **opus**:
   - **fable** (the top tier) — only where it demonstrably pays: object-level authorization (the most common blind spot), concurrency and transaction semantics, and the PR row that judges new attack surface. Always effort `max`.
   - **opus** (default) — every judgment row: anything that reads code for meaning rather than matching a pattern.
   - **sonnet** — verbatim two-sided quoting with no cross-file reasoning (#44 type drift).
   - **haiku** — mechanical grep / tool-output transcription. Its brief says: "report hits verbatim with file:line; do NOT speculate beyond the pattern list; uncertain = list under unchecked".

   Cheapness lives in the Effort column, not in a lower model: `max` only on top-tier rows; `high` = security and verdict-feeding reasoning; `medium` = ordinary judgment; `low` = transcription rows and low-yield judgment rows (readability, PR performance) where the model is kept and the depth capped.

   **Full mode — 19 rows (Phases A–K):**

   | Key | Items | Scope | Model | Effort |
   |---|---|---|---|---|
   | a1-access-control | #1 | access control, object-level authorization (IDOR) | fable | max |
   | a2-crypto-auth | #2, #4 | crypto and secret handling, authentication and sessions | opus | high |
   | a3-injection-ssrf-llm | #3, #5, #41 | injection, SSRF, AI/LLM integration | opus | high |
   | a4-endpoint-upload | #6, #7 | API endpoint and upload/parsing security | opus | high |
   | a5-container-misconfig-integrity | #8, #37, #38 | containers and secrets, misconfiguration, integrity | opus | medium |
   | a6-null-safety | #9 | null safety and boundaries (server and client) | opus | medium |
   | a7-async-concurrency | #10, #10.5 | concurrency, transactions, external API robustness | fable | max |
   | a8-logic-errors | #11, #12 | logic errors, error handling | opus | high |
   | a9-db-perf-leaks | #13, #14, #15 | query performance, leaks, complexity | opus | medium |
   | a10-bundle-deps | #16, #27 | bundle size, dependency vulnerability scan (tool output) | haiku | low |
   | a11-architecture | #17–#20 | architecture | opus | medium |
   | a12-readability-refactor | #21–#26 | readability and refactoring | opus | low |
   | a13-reuse-ops | #28–#31 | reusability, tests, configuration, operations docs | opus | medium |
   | a14a-headers-tls | #32, #35 | security headers, TLS (tool output) | haiku | low |
   | a14b-storage-protocol | #33, #34 | browser storage, messaging, service workers, protocol | opus | high |
   | a15-threat-model | #36 | design-time threat modelling | opus | high |
   | a16-migration-deploy | #39, #40 | migrations and schema drift, deploy manifests | opus | high |
   | a17-a11y-i18n | #42, #43 | accessibility, internationalization | opus | medium |
   | a18-type-drift | #44 | API ↔ client type drift | sonnet | low |

   **PR mode — conditional rows.** Classify the changed files and launch ONLY the matching rows (a small PR: 2–4 agents). The trigger classes are generic; the profile maps them to the project's paths. A file no class matches still gets `diff-bugs`.

   | Trigger class (changed files) | Key | Items | Model | Effort |
   |---|---|---|---|---|
   | always | diff-bugs | #9, #11, #12 on the diff (+ naming/DRY nits as Low) + the project's judgment gates, if any | opus | high |
   | server-side request handling: routes, controllers, handlers, services, input schemas, auth code | diff-security | #1, #3, #4, #5, #6, #7 — those with a code path through the diff; a NEW endpoint or feature also gets a #36 threat narrative | fable | max |
   | concurrency: async code, threads, pools, workers, background jobs, locks, transactions | diff-concurrency | #10, #10.5 | opus | high |
   | database: migrations, schema files, ORM models | diff-migration | #39 | opus | high |
   | client UI code | diff-frontend | #9/#10 client-side, #42/#43 on the diff | opus | medium |
   | an API schema AND client-side types in the same diff | diff-typedrift | #44 on the changed fields, both sides quoted | sonnet | low |
   | container, build, deploy or CI configuration, environment templates, proxy configuration | diff-config | #8, #32, #37, #40 | opus | high |
   | dependency manifests and lockfiles | diff-deps | #27, #38 (typosquatting, integrity) | haiku | low |
   | AI/LLM integration: provider adapters, prompt builders, chat routes | diff-llm | #41 | opus | high |
   | hot-path queries or heavy loops touched | diff-perf | #13–#15 on the diff | opus | low |

   **Delivery gates (optional).** A project may name gates in its profile: *mechanical* gates read from a script's output (`GATE <name> PASS|FAIL|N-A <note>`; a gate that cannot say is `UNMEASURED`, which stops the verdict, never passes it) and *judgment* gates that `diff-bugs` reports in its `gates` list under their exact names. The orchestrator checks the complete expected set: a **missing** gate makes the review `Incomplete`, never a pass. A gate FAIL is a finding of at least High. Shape and examples: `reference/integrations.md`.

   **Fix batches**: fix agents WRITE files, so they never run through the read-only review engine — they run through the Agent tool, only after the user approves the fix plan (`--auto-fix`), at most 4 in parallel on strictly disjoint file sets, each owning its files end to end (read → edit → type check → report).

7. **Coverage first, filter downstream** — finders report **every** issue, uncertain and low-severity ones included, each with `confidence`. They do not self-filter: an unreported finding is unrecoverable, a reported false positive costs one verification pass. A finding already defended (validation, allow-list, framework guarantee, upstream check, error handling) goes to the finder's `fp_candidates`, never silently dropped.

8. **Trust but verify + defer-vs-fix** — agents read excerpts, not whole files; before acting on a Critical/High, **open the file at the cited line and read ±30 lines**. Known false-positive patterns (the profile adds the project's own):

   | Pattern | Why flagged | Why it can be safe |
   |---|---|---|
   | a Markdown renderer given model or user text | "untrusted HTML into the page" | most Markdown renderers escape HTML by default; it is unsafe only with raw HTML explicitly enabled (grep for the option first) |
   | `<iframe sandbox="">` | "empty = lax" | the HTML specification is the inverse: an empty value applies **every** restriction |
   | string-built SQL whose pieces are all constants or validated integers | "SQL injection" | walk the taint (rule 2): a value bounded by an allow-list or a typed schema field cannot carry SQL |
   | a published container port | "exposed to the internet" | read the deployment context (a proxy, a private network, a firewall) before rating it |
   | an open read endpoint | "IDOR / horizontal escalation" | open reads can be a deliberate product decision; close it only by citing where that decision is recorded, and never by citing a defense that does not exist (an audit log that does not log reads) |

   When a Critical implies rethinking the design: verify the implementation, then the threat-model assumption, only then act.

   **Defer-vs-fix** — some findings are real but belong in a separate change: **operator action required** (an environment value, a migration, a pinned digest, a secret rotation — a separate change with a checklist); **threat-model dependent** (confirm with the owner — open read access is often the right product call; justify it from the documented trust model); **cross-system migration** (forced re-login, a data backfill — scheduled and documented with its rollback). If reporting anyway, supply a PoC that bypasses the defense; else list it as a false-positive candidate (defense-in-depth recommended).

9. **Exhaustive grep before counting** — when a finding names a count ("12 sites"), first passes undercount. Confirm with a project-wide search before sizing a fix batch, and search again in a later round: the code moves.

10. **Phase entry/exit** — at start, a row states its target file count, the known defenses it relies on and the patterns it searches. At end: files scanned, findings by severity, false positives separated, what was left unchecked and why.

11. **Token economy** — (a) section-scoped reading (the read map above); (b) structured output: finders return the engine's schema (findings / fp_candidates / unchecked / gates), and prose fields stay terse (≤120 words per finding across issue, impact and fix plan); (c) the engine's Verify phase does the per-finding check, so the orchestrator reads verdict lines, not the files; (d) scanners and other tools run inside the agents that need them, never in the orchestrator.

12. **Rounds after fixes (PR mode)** — round 1 reviews the whole diff; a later round reviews the delta since the last reviewed commit plus regressions of findings confirmed earlier, with the earlier refutations and their reasons in each brief (a finder must see a refutation to rebut it; an unsound refutation reason IS new evidence; absent new evidence, a finder does not re-raise). A record of earlier rounds is advisory, never a veto: no matching rule may dismiss a raised Critical/High without its skeptic pass. Details: `reference/integrations.md` § Rounds and ledger.

13. **Probe safety** — `Explore` blocks file edits, not shell commands, so nothing in the tool set makes a probe safe. The engine leads every finder and skeptic prompt with its probe rules (read only; every visible credential is production; start nothing — no server, container, migration, install, build or test suite; `PoC-deferred` when a reproducer cannot run inside them; secrets never reach output, logs read through a filter). The profile may add rules; nothing removes one. An agent started outside the engine (a fix-batch agent, an ad-hoc probe) has no engine to inject them: whoever writes its brief pastes them in.

---

## 3. Severity

| Severity | Definition | Criterion |
|---|---|---|
| **Critical** | security breach, data loss, outage | exploitable unauthenticated or with low privilege, large blast radius |
| **High** | attack-surface expansion, data integrity, major performance hit | an authenticated exploit, or a defense-in-depth break |
| **Medium** | maintainability, minor security, moderate performance | an indirect path or limited impact |
| **Low** | style, readability, micro-optimization | no security impact |

---

## 4. Output format

### Verdict block (MANDATORY, first lines of every report)

```
Verdict: <Merge | Merge-after-fixes | Do-not-merge | Incomplete>
Main concerns: <one line — the one or two most important findings, Critical/High count and area>
Recommended action: <one line — merge as is / merge after N fixes / rethink the design / re-run the rows that did not run>
```

Computed, and fail-closed (`scripts/verdict.py` does it from the engine's result):
- any unresolved Critical → `Do-not-merge`;
- else any row that did not give a usable answer on the commit under review (failed, missing, head mismatch), any Critical/High left unverified by the skeptic cap, any expected gate missing or `UNMEASURED` → `Incomplete` — never a pass;
- else any unresolved High (a gate FAIL included) → `Merge-after-fixes`;
- else → `Merge`.

"Unresolved" = confirmed by the Verify phase (not refuted by a strict majority of at-tier votes on the commit under review), or left unverified by the skeptic cap. A refutation that came only from below tier is advisory: the finding stands until the orchestrator rules on it by hand and says why (`verdict.py --rulings`, which prints every ruling). Findings below High are not skeptic-verified; they never block, and the orchestrator judges them with rule 8 before acting on them.

### Finding template

```markdown
[#N] [path:line] [Severity] [Category]
Issue: <description>
Trigger: <when does it fire>
PoC: <one-shot reproducer, or PoC-deferred: <why> + the static evidence>
Impact: <scope, data class, blast radius>
Existing defense: <what already stands in the way, or "none">
Fix plan: <approach>
Fix diff: <unified diff>
Regression test: <skeleton with assert lines>
CWE: e.g. CWE-89 (SQL injection)
Confidence: High/Medium/Low — <PoC run / taint traced / pattern only>
Side-effect rating: Low/Medium/High — scope: N endpoints / M modules
```

### False-positive table (per phase)

| File:line | Pattern | Existing defense | Recommended |
|---|---|---|---|

### Phase summary (per phase)

```markdown
- Files scanned: N
- Findings: Critical X / High Y / Medium Z / Low W
- FP candidates: N
- Unchecked: N (reason: …)
```

---

## 5. Phase A: Security baseline (#1–#8)

### #1 Access control and privilege escalation (OWASP A01)
Look for: every route or handler that reads or changes a resource by id — does it check that the caller may act on *that* object (owner, membership, tenant), not only that the caller is logged in? Admin-only actions reachable by ordinary users; a check on one verb (DELETE) missing on its sibling (PUT/PATCH); list endpoints that filter in the client instead of the query; ids that are guessable sequences.
Evidence: the route, the check (or its absence), and a request as user B for user A's object.

### #2 Cryptography and secret handling (OWASP A02)
Look for: secrets with a hard-coded or empty fallback (`os.getenv("KEY", "dev-secret")`) that a missing variable silently activates; weak or home-made crypto; non-constant-time comparison of tokens; secrets written to logs, error messages or URLs; randomness from a non-cryptographic source for tokens.
Evidence: the fallback path and what happens when the variable is unset (it should fail closed).

### #3 Injection (OWASP A03)
Look for: SQL, shell, template, LDAP, XPath or header injection; path traversal from user-supplied names; regular expressions with catastrophic backtracking on input (ReDoS); deserialization of untrusted data. Apply rule 2's taint walk to each.

### #4 Authentication and sessions (OWASP A07)
Look for: token validation that accepts `alg: none` or skips audience/issuer/expiry checks; refresh-token reuse not detected (a race that lets two requests both rotate); session fixation; logout that does not invalidate server-side state; password storage without a modern slow hash; missing brute-force limits; cookies without Secure/HttpOnly/SameSite where they carry a session.

### #5 Server-side request forgery (OWASP A10)
Look for: any outbound request whose URL or host comes from input; redirects followed to internal addresses; DNS rebinding (a name checked, then resolved again at connect time); internal metadata endpoints reachable. Who controls the URL decides the defense: input-controlled URLs need an allow-list and connect-time address pinning.

### #6 API endpoint security
Look for: mass assignment (a request body bound to a model with fields the caller should not set); missing rate limits on expensive or sensitive routes; verbose errors that leak stack traces, SQL or internal paths; unbounded page sizes; methods that change state on GET.

### #7 File upload and parser security
Look for: size limits enforced before reading the body; type checks by content, not extension; archive extraction without a path check (zip slip) or a size budget (zip bombs); parsers of complex formats (XML, images, documents, scientific formats) fed untrusted input without limits; stored files served back with a dangerous content type.

### #8 Containers and secret management
Look for: secrets baked into images or committed; containers running as root without need; images unpinned (`latest`); writable mounts of sensitive host paths; `.env` files in the build context; debug ports published.

## 6. Phase B: Bugs (#9–#12)

### #9 Null safety and boundaries
Look for: an empty collection reaching `max()`/`[0]`; a lookup that can return nothing used without a check; off-by-one ranges; integer overflow or float comparison; time-zone-naive dates mixed with aware ones; client code that assumes a field the API can omit.

### #10 Concurrency and transactions
Look for: check-then-act races (read, decide, write without a lock or a conditional update); shared mutable state across threads or tasks; blocking calls inside an event loop; transactions that span a network call; locks taken in inconsistent order; jobs that can run twice and are not idempotent; pools that can be exhausted by one slow caller.

### #10.5 External API robustness
Look for: calls without a timeout; retries without backoff or a bound; a failure of the remote side that leaves local state half-written; responses trusted without validation; pagination that can loop forever.

### #11 Logic errors
Look for: inverted conditions, wrong operator precedence, a branch that can never run, a state machine with an unreachable or missing transition, units mixed up, a default that silently changes behavior, copy-paste slips between similar blocks.

### #12 Error handling
Look for: exceptions swallowed (`except: pass`), errors turned into success, partial failure reported as complete, cleanup skipped on the error path, user-facing messages that leak internals, retries that hide a persistent failure.

## 7. Phase C: Performance (#13–#16)

### #13 Database query performance
Look for: N+1 queries; queries in loops; missing indexes on filtered or joined columns (check the schema, not a guess); `SELECT *` on wide tables in hot paths; unbounded result sets; counting with a full scan where an estimate would do.

### #14 Memory and resource leaks
Look for: files, sockets, cursors or processes not closed on every path; caches without a bound; listeners or timers never removed; large objects held by closures; temporary files never deleted.

### #15 Algorithmic complexity
Look for: quadratic work over input-sized collections (nested loops, repeated `in list`, string concatenation in a loop); sorting inside a loop; recomputation that could be cached; work proportional to the whole dataset per request.

### #16 Client bundle size
Look for: whole libraries imported for one function; large assets bundled instead of loaded on demand; duplicate versions of one dependency; missing code splitting on rarely used routes. (Tool output: the bundler's report, transcribed.)

## 8. Phase D: Architecture (#17–#20)

### #17 Dependencies and coupling
Look for: import cycles; modules reaching into another module's private parts; a utility module that imports application code; one change that must touch many unrelated modules.

### #18 Layering
Look for: request handlers holding business rules or raw queries; persistence code calling the web layer; domain logic depending on framework types; side effects in constructors or at import time.

### #19 API design
Look for: inconsistent naming, status codes and error shapes across endpoints; breaking changes without versioning; endpoints that leak internal models; missing pagination or filtering conventions.

### #20 Tech debt and deprecations
Look for: deprecated framework or library APIs scheduled for removal; legacy patterns the project's own conventions replace; TODOs that hide a known bug; dead feature flags.

## 9. Phase E: Readability and naming (#21–#23)

### #21 Naming
Names that mislead (a `get_` that writes, a boolean named like a noun), abbreviations a newcomer cannot read, one concept under several names.

### #22 Comments
Comments that contradict the code, describe *what* instead of *why*, or record history that belongs in version control; public interfaces without a docstring.

### #23 Function decomposition
Functions too long to hold in one's head (a rough bar: over ~80 lines, or more than ~4 nesting levels), functions doing several unrelated things, flag arguments that switch behavior.

## 10. Phase F: Refactoring (#24–#27)

### #24 Duplication
The same logic in several places that must change together; near-copies that have already drifted apart.

### #25 Design patterns
A hand-rolled mechanism where the framework or standard library has one; a pattern applied where a function would do.

### #26 Legacy modernization
Old language or framework idioms the codebase's newer code has moved away from; compatibility shims for versions no longer supported.

### #27 Dependency vulnerability scan
Run the stack's scanner in a mode that installs nothing (for example `pip-audit --no-deps --disable-pip -r <requirements>`, `npm audit --package-lock-only`, `osv-scanner --lockfile <file>`) inside the agent and transcribe its findings with versions and fixed-in versions. A scanner that is not installed, or that would need to install or build, is listed under unchecked, never reported as clean.

## 11. Phase G: Reusability and operations (#28–#31)

### #28 Utility extraction
Helpers buried in one module that others re-implement; shared logic without a home.

### #29 Test coverage
Critical paths (authorization, money, data deletion, parsing of untrusted input) without tests; tests that assert nothing meaningful; tests that depend on order or on the network. Read the tests; do not run them (rule 13).

### #30 Externalized configuration
Hard-coded hosts, ports, paths, limits and credentials that should come from configuration; configuration read in many places instead of one; missing validation of configuration at start-up.

### #31 Operations documentation
How to deploy, roll back, rotate a secret, restore a backup, and what each environment variable means — present, current and findable.

## 12. Phase H: Dynamic, protocol and browser (#32–#35)

### #32 Browser security headers
Content-Security-Policy, Strict-Transport-Security, X-Content-Type-Options, frame protections, Referrer-Policy, Permissions-Policy — set where the responses are served (the application or its proxy). Transcribe from configuration or from a read-only request to a deployment the profile names.

### #33 Browser storage, messaging and service workers
Tokens in `localStorage`; `postMessage` handlers that do not check the origin; service workers caching authenticated responses; sensitive data left in client storage after logout.

### #34 Protocol issues
Request smuggling between a proxy and the application; cache poisoning through unkeyed headers; CORS that reflects any origin with credentials; host-header trust.

### #35 TLS and certificates
Protocol versions and ciphers, certificate validation on outbound calls (no `verify=False`), HSTS. Transcribe from configuration or a scanner the profile names.

## 13. Phase I: OWASP additional categories (#36–#38)

### #36 Insecure design (OWASP A04)
For a new feature or endpoint: who can call it, with what, and what is the worst they can do with it? Abuse cases, missing limits on business actions, trust placed in the client, a flow that can be replayed or reordered.

### #37 Security misconfiguration (OWASP A05)
Debug modes on in production configuration; default credentials; interactive API docs or admin consoles exposed where they should not be; overly broad CORS; missing database timeouts.

### #38 Software and data integrity (OWASP A08)
Deserialization of untrusted data; `eval` of fetched code; dependencies without lockfile hashes; suspicious package names (typosquatting); update or plugin mechanisms without signature checks.

## 14. Phase J: Data and delivery integrity (#39–#41)

### #39 Migration safety and schema drift
Migrations that lock large tables, drop data without a backup path, or cannot be rolled back; a migration and the schema snapshot (or ORM models) that disagree; numbering collisions with the base branch; a schema change without the matching change everywhere the project keeps the schema.

### #40 Deploy manifest hardening
Containers without resource limits, probes or a non-root user; secrets in plain manifests; privileged pods or host networking without need; images by mutable tag.

### #41 AI / LLM integration security
Prompt injection from retrieved or user content reaching tools or actions; model output rendered as HTML or executed; secrets or personal data sent to a provider; tool calls without an allow-list; unbounded cost (no token or rate limits).

## 15. Phase K: Surface contracts (#42–#44)

### #42 Accessibility
Interactive elements without accessible names; keyboard traps; focus not managed in dialogs; colour contrast; images without alternatives; forms without labels.

### #43 Internationalization
Hard-coded user-facing strings; keys present in one language file and missing in another; dates, numbers and plurals formatted by hand.

### #44 API ↔ client type drift
Fields the server sends or requires that the client types omit, or the reverse; enum values out of sync; nullable on one side only. Quote both sides verbatim.

---

## 16. Final summary (full mode)

1. **Severity rollup** — a table of phases × severities, with false positives and unchecked counts.
2. **Priority list** — every confirmed Critical/High with its fix plan, in the order to fix.
3. **False-positive list** — each with the evidence that refuted it.
4. **Roadmap** — what to fix now, next release, later; what needs the owner's decision (defer-vs-fix).
5. **Out-of-scope observations** — what a row noticed outside its items.
6. **Self-assessment** — which rows ran on which tier (substitutions named), what was unchecked and why, and what the next sweep should read first.
