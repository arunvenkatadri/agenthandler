# Bug scan — September 4, 2026

Reviewed current GitHub `main` at `4645e62901382338bf724603c69d8f3f4985453a`,
after updating the local checkout from twelve commits behind. This was a focused
scan of authentication, supervision results, CI and review enforcement, with
the complete existing suite run as a baseline; it is not an exhaustive security audit.

## Fixed

| Finding | Impact | Regression coverage |
| --- | --- | --- |
| `require_auth=True` installed the literal `__REJECT_ALL__` as a valid key | A caller could authenticate to an unconfigured control plane with that public string | HTTP and WebSocket reject that value and arbitrary/missing tokens |
| OAuth states and session tokens shared the same dictionary | An unauthenticated `/auth/login` caller could use `_state_<state>` as a bearer token | Login challenges cannot authenticate to HTTP, `/auth/me`, or WebSocket endpoints |
| OAuth state lacked browser binding; sessions were process-global | A challenge could be completed from another browser, and one app's session could authenticate to another app | Cookie binding, single-use and expiry checks, and app isolation |
| WebSocket authentication checked only API keys | OAuth-only apps exposed session audit events without authentication and rejected legitimate OAuth sessions in required-auth mode | Anonymous rejection, valid OAuth/API-key access, expiry, malformed token handling |
| OAuth token exchange omitted the callback URI | Providers requiring `redirect_uri` at token exchange could reject valid logins | Mock provider checks the callback URI; URL parameters are encoded |
| Compression reconstructed results without `succeeded` or `tool_name` | Successful compressed calls appeared to fail; `always` mode still skipped short outputs | Success metadata, short-output mode behavior, errors and compressor failure |
| No branch protection; Claude workflow triggered from PRs/comments | CI was advisory; adding an API key could enable contributor-triggered spending | Gate tests cover identity, both providers, stale/dismissed reviews, owner approval, API failure, pagination and head changes; workflow tests cover paid trigger restrictions |

## Validation

- Baseline: 706 tests passed once localhost server tests were allowed to bind.
- Expanded suite: 752 tests passed; 78.27% total coverage, 92% authentication coverage.
- Ruff lint and format, strict mypy, and actionlint passed.
- Source distribution and wheel build successfully and pass `twine check`.
- OAuth providers and compression are mocked: no live provider calls or API charges.

OAuth sessions and login states remain in memory. Deployments requiring shared
sessions across workers need a separate shared-session-store design. This change
does not introduce OAuth user/organization authorization rules: deployments must
still choose who is entitled to control their agents.

## Follow-up scan and proposed fixes

The broader follow-up used merged `main` at
`2989eb75d590dd80dfb2cc6e82cfb52e334e5b4b` as its baseline. The fixes below are
in **draft PRs**, not deployed or merged. This supersedes the earlier note about
OAuth authorization: PR #10 introduces an explicit provider-ID allowlist.

| PR | Confirmed problems addressed | Regression coverage |
| --- | --- | --- |
| [#5 — Approval isolation](https://github.com/arunvenkatadri/agenthandler/pull/5) | Replayed and cross-session approvals; mutable request snapshots; shared confirmation policy temporarily disabled during approved execution; full queues escaping supervision | Concurrent calls, single-use consumption, session binding, nested snapshot mutation, queue saturation |
| [#6 — Durable checkpoints](https://github.com/arunvenkatadri/agenthandler/pull/6) | SQLite and auto-checkpoints dropped checksums/resume counters; stateless payloads reached disk; stopped/replaced supervisors stayed usable; audit entries duplicated or disappeared | Legacy migrations, tool-call/restart integrity checks, crash-loop limits, stateless disk isolation, retained references, audit continuity |
| [#7 — Execution safeguards](https://github.com/arunvenkatadri/agenthandler/pull/7) | Ineffective silence/sync/request deadlines; concurrent recovery probes and stale breaker outcomes; exhausted budgets permitting execution; cache bypass of redaction/post-guards and session isolation; invalid token counts; scope defaults/typos; guardrail errors failing open; URL blocklist evasion using ports/userinfo | Real blocking threads, cancellation, concurrent recovery, cached privacy and isolation, budget persistence, callable defaults, guardrail failure/timeout, nested URL arguments |
| [#8 — Database connectors](https://github.com/arunvenkatadri/agenthandler/pull/8) | Read-only SELECT INTO bypass; RETURNING writes not committed; errors leaving transactions unusable; incorrect parameter/URI handling; invalid result limits; synchronous driver work blocking the loop | SQLite transaction/URI integration tests, mocked PostgreSQL/MySQL settings, row limits, Mongo negative limits, event-loop responsiveness |
| [#9 — Scheduling](https://github.com/arunvenkatadri/agenthandler/pull/9) | Daily cron fired only once; incorrect weekday/day-field/step semantics; incomplete validation; unsafe scheduler shutdown; ineffective webhook disable | Consecutive days, Sunday aliases, invalid fields, running-job cancellation, restart, HTTP controls, app shutdown |
| [#10 — OAuth authorization](https://github.com/arunvenkatadri/agenthandler/pull/10) | Any provider user could control agents; partial OAuth settings could leave the app public | Stable allowed IDs, denied identities on HTTP/WebSocket, renamed users, malformed profiles, missing/partial configuration |
| [#11 — Integration behavior](https://github.com/arunvenkatadri/agenthandler/pull/11) | A2A cancellation only changed a label; duplicate task IDs; conditional approvals stopped their own session; pipeline limits escaped and failures stopped borrowed sessions; routing insertion order and client cleanup; invalid context limits | Running-task cancellation, duplicate IDs, conditional approval execution, iteration failures, borrowed sessions, routing round trips and mocked provider cleanup |
| [#12 — Streaming delivery](https://github.com/arunvenkatadri/agenthandler/pull/12) | Kafka/Kinesis advanced past failed handlers; Redis never revisited pending messages and hid startup failures | Simulated fail-then-succeed delivery and acknowledgement ordering for all three brokers |
| [#13 — Dashboard escaping](https://github.com/arunvenkatadri/agenthandler/pull/13) | Data could escape HTML attributes or inline JavaScript arguments; routing fields rendered unescaped | Full-script parsing, hostile quote/markup inputs, actual agent-card handler execution, routing editor/preview output; Node tests added to CI |

PR #7 is stacked on #5 because both change the confirmation preflight path.
Merge #5 first, then retarget #7 to `main` before merging it. The remaining PRs
are based on `main`; their combined changes were checked on the local
`verify/bug-scan-integration` branch. GitHub `main` was not changed by this scan.

### Combined validation

- **839 Python tests passed**, up from 753 on the merged baseline: 86 added cases.
- **Five dashboard JavaScript tests passed**, with no npm dependencies.
- **82.20% Python statement coverage**; the CI minimum remains 75%.
- Ruff lint/format, strict mypy, and actionlint passed on the combined tree.
- Thirteen selected new regression cases failed against an isolated archive of
  baseline `main`; all pass with the proposed fixes. These include authorization,
  approval binding, persistence, deadlines, database transactions, cron, routing,
  and stream delivery.
- GitHub CI runs the Python 3.10–3.13 matrix, lint, typing, and package checks.
  The required `Review policy` status remains pending while PRs are drafts and
  have no qualifying AI review.
- No paid AI review, OAuth provider login, model API call, or live external
  database/broker connection was used for validation. The federation exchange
  remains untested against Anthropic; its configuration is documented separately.

### Scope and practical limits

This pass examined execution/approval boundaries, session persistence, auth,
connectors, scheduling, orchestration, routing/context, streams, dashboard
rendering, and CI/review enforcement. It is a repository bug scan with regression
coverage, not a claim that every path is free of defects. Optional SDK/MCP,
compression providers, and external services still need deployment integration
testing; browser checks here exercise JavaScript/rendering without a live browser.

Compatibility changes are intentional: approvals authorize one attempt;
unknown scope constraints and nonpositive context/result limits are rejected;
configured guardrails fail closed on errors; and OAuth deployments must configure
`AGENTHANDLER_OAUTH_ALLOWED_SUBJECTS` (see [OAuth setup](oauth-authorization.md)).

Python cannot forcibly kill a running worker thread. A timed-out synchronous
tool/database operation may still finish its side effects. Session stop blocks
future calls and retires checkpoint writers; it does not undo in-flight work.
Use idempotency and provider-native cancellation where needed.

SQL filtering and read-only transactions do not replace restricted database
credentials. Result caps bound returned data, not query work. Legacy checkpoints
without checksums remain readable; set a secret `AGENTHANDLER_POLICY_KEY` for
meaningful policy-integrity protection. OAuth sessions are app-local and do not
provide shared-worker sessions, per-user roles, or organization membership checks.

Streaming retries can duplicate deliveries and stall on permanently failing
handlers. Applications need idempotency and a dead-letter strategy. Redis pending
recovery covers the configured consumer name; Kinesis still lacks durable restart
checkpoints and reshard discovery. These are explicit service-integration limits,
not validated production delivery guarantees.
