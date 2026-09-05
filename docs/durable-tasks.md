# Durable tasks and verified completion

`DurableTaskRunner` is an optional, sequential orchestration layer above
`SessionManager` and `Supervisor`. It has no model-provider dependency. Use it
when a job needs to survive a fresh conversation, worker restart, or process death.
The existing `Pipeline` and `ReflectionLoop` do not become durable automatically.

## Completion states

`CompletionStatus` distinguishes `running`, `proposed`, `verified`, `invalid`, and
`blocked`. Only `verified` means `completed == True`.

**ReflectionLoop behavior change:** a model's `done: true` now produces a
`proposed` result unless you provide an async `verifier`. Missing tools, malformed
JSON, non-object JSON, and non-boolean completion flags never mean success.
The think phase can explicitly return `blocked: true`, `done: false`, `tool: null`,
and a nonempty `reason`. A reflection-phase stop also means blocked work.

```python
from pathlib import Path
from agenthandler import VerificationResult

async def verify_report(proposal):
    # The application fixes the acceptance target; do not trust a model-supplied
    # path or merely ask the same model whether it succeeded.
    path = Path("report.txt")
    passed = path.is_file() and "Required conclusion" in path.read_text()
    return VerificationResult(passed, {"report_checked": str(path)}, "Checked report")

# Pass verifier=verify_report to ReflectionLoop(...).
# result.final_answer remains available for unverified proposals.
```

The verifier receives a detached proposal. It must return `VerificationResult`
with a real boolean and JSON evidence; successful verification requires nonempty
evidence. A rejection is blocked work; an invalid verifier response is invalid
completion. ReflectionLoop's model and verifier callables still need their own
supervision/budget wrappers when appropriate. The durable runner below supervises
and reserves a budget for all three callback phases.

## Define an acceptance contract

```python
from agenthandler import (
    DurableTaskRunner, Milestone, RecoveryResult, SessionManager,
    SqliteStore, SqliteTaskStore, TaskLimits, VerificationResult,
)

async def write_report(ctx):
    # ctx.inputs contains the original request. ctx.outputs contains outputs of
    # verified earlier milestones. Neither can mutate the persistent task.
    return {"report": "Required conclusion", "operation_id": ctx.operation_id}

async def check_report(ctx):
    passed = ctx.output["report"] == "Required conclusion"
    return VerificationResult(passed, {"checked_report": ctx.output["report"]})

steps = [Milestone("report", "Report contains the required conclusion",
                   write_report, check_report)]
runner = DurableTaskRunner(
    SessionManager(SqliteStore("sessions.db")),
    SqliteTaskStore("tasks.db"),
)
task = runner.create("writer", "Write the required report", steps,
                     limits=TaskLimits(max_calls=10))
# Persist task.task_id in the application that owns this job.
result = await runner.run(task.task_id, steps)
assert result.completed
```

After a restart, reconstruct the runner using the same two databases and supply
the same milestone definitions to `run(task_id, steps)`. Do not call `create`
again for the same job: it creates a new job with a new budget.

Acceptance criteria, milestone order, versions, and budget allowances are stored
at creation; a changed specification is rejected on resume. Increment a
milestone's `version` when its callback semantics change. Python code itself is
not serialized or hashed. Callback registration and the databases are trusted
application state; do not expose them as agent-writable tools. Session payload
updates cannot change the task's requirements, evidence, or budget.

All milestones must pass their validators before the task is verified. For
cross-milestone requirements or artifacts that can change over time, include a
final milestone whose validator checks the entire delivered result. Evidence is
a record of a check at execution time, not a promise that an external artifact
can never change. A completed task returns its stored result without new calls.

## Recover uncertain operations

Before executing a milestone, the runner commits its stable `operation_id`,
pending state, and budget reservation. Successful output is committed before
verification. Verified milestones are skipped on resume; an executed milestone
whose verification was interrupted is verified again without repeating its action.

A pending operation may already have taken effect. Supply a `reconcile(ctx)`
callback that looks up the operation in the authoritative external system:

- `RecoveryResult("completed", output)` recovers the result and proceeds to verification.
- `RecoveryResult("absent")` certifies it never executed and cannot execute later;
  the runner may invoke the action again with the same operation ID.
- `RecoveryResult("unknown", reason="...")` blocks execution without replay.

No reconciler also blocks replay. A timeout or eventually consistent lookup miss
does not prove absence. Use the operation ID as the provider's idempotency key
where supported. This protocol does not promise exactly-once execution by an
arbitrary external service. Reconciliation and verification must themselves be
safe to repeat. Exceptions, cancellation, and non-JSON action outputs may leave
an uncertain operation requiring reconciliation.

Each invocation passes through Supervisor under `task.<milestone-id>.execute`,
`.recover`, or `.verify`. Policies and guardrails apply to these callback names;
the adapter is responsible for supervising any nested tools it calls. A denial
blocks the job. Supervisor-cached or fallback output cannot manufacture a
successful callback result, and deferred approvals cannot execute a callback
after its worker lock is released. Arrange required authorization before running
the task; this runner does not implement durable approval continuation.

A paused/stopped session is not automatically authorized by a fresh worker. The
application must explicitly resume it through SessionManager before the task
can continue. Blocked tasks may be retried with the same ID after resolving the
cause; all prior reservations remain charged.

## Budgets and concurrency

`TaskLimits` sets lifetime call, token, and microUSD limits. `CallBudget` on each
milestone supplies `action_budget`, `verification_budget`, and `recovery_budget`.
Every callback reserves one call and its declared tokens/cost **before** execution.
Reservations are never refunded, even after a crash. Recovery and validation
consume the same lifetime budget. One dollar is 1,000,000 microUSD.

These are conservative reservations, not measured provider invoices. **Adapters
must enforce their declared upper bounds at the provider**, including input and
output tokens, retries, and other billable operations. Zero means no consumption
of that resource; it does not grant unlimited model usage. The runner cannot
enforce an arbitrary callable's external spending. Provider/model integration
stays in adapters, outside the dependency-free core.

`SqliteTaskStore` allows one active runner per store, across threads/processes.
A second worker gets `TaskBusyError`. A separate SQLite worker-lock database
holds the lock while the task database commits progress. Process death releases
the worker lock without undoing reservations. Keep both files on a local disk;
do not delete or replace either database while workers are active. Use separate
stores when independent jobs need concurrent execution.

## Validation

Run `pytest tests/test_completion.py tests/test_task.py`. The tests use fake
providers, fresh managers, cancellations, concurrent workers, and a subprocess
that exits immediately after committing an external receipt. They check recovery,
no repeated effects, persistent budgets, immutable acceptance criteria, and
rejection of unsupported completion claims. Existing CI discovers them normally;
no paid model calls or new automatic evaluation triggers are introduced.
