# Required verification gates

Use `VerificationGate` around an existing agent execution loop when only trusted,
independently observed acceptance checks may mark an artifact verified. A model's
final answer is a proposal, even when it says its tests passed.

```python
from agenthandler import VerificationGate, VerificationResult

async def regression(context):
    # These are application-owned functions. They execute the same regression
    # test against the original and patched snapshots, outside the author's
    # process, and inspect actual test results (not the author's report).
    baseline = await run_regression(context["baseline_snapshot"])
    patched = await run_regression(context["patched_snapshot"])
    passed = baseline.assertion_failed and patched.all_tests_passed
    return VerificationResult(passed, {
        "attempt_id": context["attempt_id"],
        "baseline_digest": baseline.snapshot_digest,
        "patched_digest": patched.snapshot_digest,
        "baseline": baseline.to_dict(),
        "patched": patched.to_dict(),
    }, "Independent regression check")

gate = VerificationGate(required_stages=("regression",))
report = await gate.run(
    {"attempt_id": attempt_id,
     "baseline_snapshot": original_snapshot,
     "patched_snapshot": proposed_snapshot},
    validators={"regression": regression},
    timeout_seconds=120,
)
if report.verified:
    await publish_proposed_fix(proposed_snapshot, report.to_dict())
else:
    await record_unverified_findings(report.to_dict())
```

`run_regression`, snapshotting, and publication in this example belong to the
application. Pin the tested artifacts and publish those exact artifacts; changing
files after verification invalidates the evidence. A passing check supports only
its configured acceptance contract, not a general claim of system correctness.
Validators must distinguish a genuine expected regression failure from collection
errors, missing dependencies, skipped tests, and unrelated failures.

## Outcomes and enforcement

- `verified`: every required named validator ran and returned a valid
  `VerificationResult(True, nonempty_json_evidence)`.
- `unverified`: no stages were required, or a required validator was missing.
  Missing registration prevents all validators from running.
- `failed`: a validator rejected the artifact, raised an exception, timed out, or
  returned invalid evidence. Later dependent stages remain unverified.

Required stages are fixed when the gate is constructed. Agent output cannot
remove a requirement. Each validator receives its own detached JSON context.
Reports copy and freeze the stage mapping and nested evidence before the next
validator executes; callers cannot change a recorded verdict or its evidence. Extra
registered validators do not replace required ones. `report.to_dict()` returns a
JSON snapshot; `report.as_result()` adapts the report to a verifier expecting
`VerificationResult`.

The timeout covers the entire gate invocation. Cancellation propagates, so the
owning worker can stop cleanly. Validators must support cancellation and terminate
their subprocesses. The gate cannot preempt arbitrary blocking Python code. The
application must isolate untrusted test code from credentials, gate state, and
publication privileges. A validator is trusted application code; the gate cannot
make a validator that merely repeats an author's claims independent.

## Restarts and persistence

Every invocation executes its validators afresh. The gate never imports a saved
`verified` flag, an author's final answer, or a partially completed report as
proof. If a worker dies during verification, run the complete gate again against
the recovered, pinned artifacts. Include the job/attempt ID and artifact digest in
evidence. Persist reports outside any location the agent can write. Reports are
records of observations, not reusable authorization tokens.

This gate is deliberately stateless: it does not restart cloud workers, preserve
an agent conversation, or make external operations exactly once. The owning
application must preserve job IDs, claim exclusive ownership, maintain budgets,
and reconcile uncertain external effects before retrying them. For example, a
crash after pushing a branch must trigger a remote commit lookup before another
push; a missing eventually consistent result does not prove the push never ran.

AgentHandler's `SessionManager` supplies session checkpoint/resume primitives.
It does not make an arbitrary external SDK loop or its effects durable. The
separate `DurableTaskRunner` work on the development branch adds milestone intent
and recovery coordination; it is not part of this minimal verification change.

Run `pytest tests/test_verification.py` for deterministic acceptance, deadline,
cancellation, and fresh-attempt tests; no model provider is required.
