import asyncio

import pytest

from agenthandler import (
    DurableTaskRunner,
    Milestone,
    SessionManager,
    SqliteStore,
    SqliteTaskStore,
    VerificationGate,
    VerificationResult,
)


@pytest.mark.asyncio
async def test_interrupted_gate_rechecks_without_reauthoring(tmp_path):
    effects, checks = [], []
    interrupted = False

    def runner():
        return DurableTaskRunner(
            SessionManager(SqliteStore(str(tmp_path / "sessions.db"))),
            SqliteTaskStore(str(tmp_path / "tasks.db")),
        )

    async def execute(ctx):
        effects.append(ctx.operation_id)
        return {"author_claim": "verified", "artifact_digest": "fixed-snapshot"}

    async def baseline(context):
        checks.append("baseline")
        return VerificationResult(True, {"digest": context["artifact_digest"], "exit_code": 1})

    async def patched(context):
        nonlocal interrupted
        checks.append("patched")
        if not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return VerificationResult(True, {"digest": context["artifact_digest"], "exit_code": 0})

    async def verify(ctx):
        report = await VerificationGate(("baseline", "patched")).run(
            ctx.output, validators={"baseline": baseline, "patched": patched}
        )
        return report.as_result()

    steps = [Milestone("fix", "Independent regression evidence", execute, verify)]
    first = runner()
    task = first.create("test", "Fix bug", steps)
    with pytest.raises(asyncio.CancelledError):
        await first.run(task.task_id, steps)
    checkpoint = first.store.load(task.task_id)
    assert not checkpoint.completed
    assert checkpoint.milestones["fix"]["state"] == "executed"
    assert checkpoint.calls_reserved == 2
    assert "verification" not in checkpoint.milestones["fix"]
    restored = await runner().run(task.task_id, steps)
    assert restored.completed
    assert restored.calls_reserved == 3
    assert len(effects) == 1
    assert checks == ["baseline", "patched", "baseline", "patched"]
