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
