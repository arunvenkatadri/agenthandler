---
name: agenthandler
description: Use when adding guardrails to an AI agent's tool calls in Python — iteration caps, token or dollar budgets, per-tool timeouts, human approval gates, circuit breakers, or audit logging — or when working with the agenthandler library.
---

# AgentHandler

Governance for agent tool calls. `Supervisor.call()` wraps any async callable with
timeouts, budgets, circuit breakers, guardrails and an audit record. It supervises
the calls your loop makes — it does not run the loop.

Works alongside any framework (OpenAI Agents SDK, Claude Agent SDK, LangChain,
CrewAI, a hand-written loop). You keep your loop; you route tool calls through the
supervisor.

```bash
pip install agenthandler
```

## The shape

```python
from agenthandler import AgentHandlerError, ApprovalQueue, Policy, Supervisor

policy = Policy(
    max_iterations=6,                    # rounds, enforced by record_iteration()
    token_budget=10_000,                 # enforced by record_tokens()
    tool_timeout=5.0,                    # seconds, per call
    tool_timeouts={"delete_cache": 2.0}, # per-tool override
    require_confirm=["delete_cache"],    # needs a human first
)

approvals = ApprovalQueue()
sv = Supervisor(
    policy,
    session_id="run-1",        # REQUIRED for require_confirm to gate rather than deny
    approval_queue=approvals,  # REQUIRED for the same reason
)

while True:
    try:
        sv.record_iteration()          # raises at the cap
    except AgentHandlerError as e:
        break

    step = await call_model(messages)

    try:
        sv.record_tokens(step["tokens"])  # raises over budget
    except AgentHandlerError as e:
        break

    result = await sv.call(step["tool"], TOOLS[step["tool"]], **step["args"])
    if not result.succeeded:              # never raises — check the result
        ...
    messages.append({"role": "tool", "content": result.user_message()})

snapshot = sv.finish()
```

## Two error styles, deliberately

| Call | On limit |
|---|---|
| `sv.call(...)` | **Never raises.** Returns a `SupervisedResult`; check `result.succeeded` |
| `sv.record_iteration()` | **Raises** `AgentHandlerError` |
| `sv.record_tokens(n)` | **Raises** `AgentHandlerError` |

A loop needs both `if not result.succeeded` and `try/except`. Using one style for
everything will silently skip a limit.

`record_iteration()` and `record_tokens()` are explicit because AgentHandler does
not run your loop: one `sv.call()` is not necessarily one round, and only you know
what your model call actually spent.

## Approval gates

`require_confirm` alone is **not** a gate. Without both `session_id=` and
`approval_queue=` on the `Supervisor`, the tool is denied outright, every time,
with no approval ever queued.

The queued call returns a failed result, not an exception:

```python
result = await sv.call(name, fn, **kwargs)

if result.error is not None and result.error.kind == "approval_pending":
    approval_id = result.error.details["approval_id"]
    request = approvals.get(approval_id)

    if operator_says_yes(request):
        approvals.approve(approval_id, approved_by="operator")
        result = await sv.call_approved(approval_id, name, fn, **kwargs)
    else:
        approvals.deny(approval_id, reason="operator declined")
```

**Use `call_approved`, not `execute_approved`.** `call_approved` re-runs every check
and consumes the approval, so one approval authorizes exactly one call. It requires
`kwargs` to match the original call exactly. `execute_approved` replays stored args
without consuming the approval, and bypasses the gate by temporarily mutating
`policy.require_confirm` in place — unsafe if a `Policy` is shared across concurrent
supervisors.

## Streams

Subscribe an agent to a Kafka topic; each message runs through supervision, so a
traffic spike hits the iteration cap instead of the invoice.

```python
from agenthandler import KafkaStreamConnector

async def on_message(payload):
    return await pipeline.run()

connector = KafkaStreamConnector(
    bootstrap_servers="localhost:9092", topic="events",
    group_id="my-agent", on_message=on_message,
)
await connector.start()
```

Kafka commits the handled offset, so a restart resumes where it left off. **Kinesis
keeps no durable position** — it resumes from `shard_iterator_type` and never
notices a reshard, so use it for live tailing only. A failed handler retries
indefinitely with no dead-letter queue, so a poison message stalls its partition.

## Audit and metrics

```python
from agenthandler import AuditLog, AuditSink, Observer

observer = Observer()
sv = Supervisor(policy, audit=AuditLog("run-1", [sink]), observer=observer)
```

`Policy.audit_enabled=True` is the default but is a **no-op unless you pass
`audit=`** — otherwise the supervisor uses `AuditLog.noop()`.

Per-tool metrics keys are `calls`, `successes`, `failures` — not `total_calls`,
which is the key on the *session*-level summary. Mixing them up raises `KeyError`.

Calls rejected before execution (approval-pending, scope denial, open breaker)
never reach the `Observer`. The audit log is the only place blocked traffic appears.

## Common mistakes

| Mistake | What happens |
|---|---|
| `require_confirm` without `session_id=` and `approval_queue=` | Tool denied forever; no approval is ever queued |
| Wrapping the model call instead of tool calls | Nothing is supervised; budgets never fire |
| `try/except` around `sv.call()` only | Tool failures silently ignored — it returns, never raises |
| Checking `result.succeeded` only | Iteration and token limits never caught — they raise |
| `execute_approved` in the approve path | Approval not consumed; mutates shared policy state |
| `metrics["total_calls"]` per tool | `KeyError` — per-tool keys are `calls`/`successes`/`failures` |
| Expecting `audit_enabled` to produce a log | No-op without `audit=` |
| Kinesis for anything that must not miss records | No durable position; skips everything during a restart |

## Reference

- Control plane: `uvicorn agenthandler.server:app`. Requires `AGENTHANDLER_API_KEY`,
  or OAuth with `AGENTHANDLER_OAUTH_ALLOWED_SUBJECTS` — the server refuses to start
  without an allow-list, since completing an OAuth flow proves identity, not entitlement.
- Also in the package: `SupervisedMCPServer` (expose supervised tools over MCP),
  `ModelRouter`, `SessionReplay`, `CompressedSupervisor`, circuit breakers,
  16 guardrail classes, durable sessions via `SqliteStore`.
- Source and full docs: https://github.com/arunvenkatadri/agenthandler
