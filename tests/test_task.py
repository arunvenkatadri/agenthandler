"""Deterministic lifecycle tests; no model SDK, network or paid evaluations."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agenthandler import (
    CallBudget,
    DurableTaskRunner,
    Milestone,
    RecoveryResult,
    SessionManager,
    SqliteStore,
    SqliteTaskStore,
    TaskBusyError,
    TaskLimits,
    VerificationResult,
)


def runner_at(path):
    return DurableTaskRunner(
        SessionManager(SqliteStore(str(path / "sessions.db"))),
        SqliteTaskStore(str(path / "tasks.db")),
    )


async def action(ctx):
    return {"artifact": "report", "operation_id": ctx.operation_id}


async def verify(ctx):
    return VerificationResult(ctx.output["artifact"] == "report", {"checked": ctx.output})


def milestone(**kwargs):
    return Milestone("report", "Report exists", action, verify, **kwargs)


async def test_complete_and_resume_without_replaying(tmp_path):
    runner = runner_at(tmp_path)
    steps = [milestone()]
    task = runner.create("agent", "Write report", steps)
    result = await runner.run(task.task_id, steps)
    assert result.completed
    assert result.next_milestone is None
    assert result.calls_reserved == 2
    assert runner.manager.status(task.session_id).status.value == "stopped"
    assert runner.manager.get_supervisor(task.session_id) is None
    assert (
        result.milestones["report"]["verification"]["evidence"]["checked"]["artifact"] == "report"
    )

    async def never(ctx):
        pytest.fail("Completed task executed again")

    fresh = runner_at(tmp_path)
    replay = await fresh.run(task.task_id, [replace(steps[0], execute=never, verify=never)])
    assert replay.completed
    assert replay.calls_reserved == 2


async def test_resume_repairs_crash_between_completion_and_session_cleanup(tmp_path, monkeypatch):
    runner = runner_at(tmp_path)
    step = milestone()
    task = runner.create("agent", "Write report", [step])

    def crash(session_id):
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner.manager, "stop", crash)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(task.task_id, [step])
    assert runner.store.load(task.task_id).completed
    fresh = runner_at(tmp_path)
    result = await fresh.run(task.task_id, [step])
    assert result.completed
    assert result.calls_reserved == 2
    assert fresh.manager.status(task.session_id).status.value == "stopped"


async def test_fresh_context_resumes_next_milestone_with_original_inputs(tmp_path):
    seen = []

    async def first(ctx):
        ctx.inputs["request"] = "agent tried to change requirements"
        return {"artifact": "report"}

    async def second(ctx):
        seen.append(ctx)
        if len(seen) == 1:
            raise asyncio.CancelledError()
        return {"artifact": ctx.outputs["first"]["artifact"]}

    async def reconcile(ctx):
        return RecoveryResult("absent")

    steps = [
        Milestone("first", "Report exists", first, verify),
        Milestone("second", "Report delivered", second, verify, reconcile),
    ]
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write and deliver", steps, inputs={"request": "original"})
    with pytest.raises(asyncio.CancelledError):
        await runner.run(task.task_id, steps)
    saved = runner.store.load(task.task_id)
    assert saved.next_milestone == "second"
    assert saved.calls_reserved == 3
    assert saved.inputs == {"request": "original"}
    result = await runner_at(tmp_path).run(task.task_id, steps)
    assert result.completed
    assert result.calls_reserved == 6
    assert seen[-1].inputs == {"request": "original"}
    assert seen[-1].outputs == {"first": {"artifact": "report"}}
    assert seen[0].operation_id == seen[-1].operation_id


@pytest.mark.parametrize("recovery", [None, RecoveryResult("unknown")])
async def test_uncertain_effect_is_not_replayed(tmp_path, recovery):
    effects = []

    async def crash(ctx):
        effects.append(ctx.operation_id)
        raise asyncio.CancelledError()

    async def reconcile(ctx):
        return recovery

    steps = [replace(milestone(), execute=crash, reconcile=reconcile if recovery else None)]
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", steps)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(task.task_id, steps)
    for _ in range(2):
        result = await runner_at(tmp_path).run(task.task_id, steps)
        assert result.status == "blocked"
        assert not result.completed
    assert len(effects) == 1


async def test_crash_in_verification_does_not_repeat_action(tmp_path):
    effects = []

    async def execute(ctx):
        effects.append(ctx.operation_id)
        return await action(ctx)

    async def crash(ctx):
        raise asyncio.CancelledError()

    step = replace(milestone(), execute=execute, verify=crash)
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step])
    with pytest.raises(asyncio.CancelledError):
        await runner.run(task.task_id, [step])
    saved = runner.store.load(task.task_id)
    assert saved.status == "proposed"
    assert saved.milestones["report"]["state"] == "executed"
    result = await runner_at(tmp_path).run(task.task_id, [replace(step, verify=verify)])
    assert result.completed
    assert len(effects) == 1
    assert result.calls_reserved == 3


@pytest.mark.parametrize("kind", ["calls", "tokens", "cost"])
async def test_budget_survives_crash_and_repeated_resume(tmp_path, kind):
    async def crash(ctx):
        raise asyncio.CancelledError()

    async def reconcile(ctx):
        pytest.fail("Recovery exceeded the persistent budget")

    budget = CallBudget(tokens=10, cost_microusd=20)
    step = replace(
        milestone(),
        execute=crash,
        reconcile=reconcile,
        action_budget=budget,
        recovery_budget=budget,
    )
    limits = {
        "calls": TaskLimits(max_calls=1),
        "tokens": TaskLimits(max_tokens=10),
        "cost": TaskLimits(max_cost_microusd=20),
    }[kind]
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step], limits=limits)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(task.task_id, [step])
    for _ in range(2):
        result = await runner_at(tmp_path).run(task.task_id, [step])
        assert result.status == "blocked"
        assert result.reason == "Task budget exhausted"
        assert result.calls_reserved == 1
        assert result.tokens_reserved == 10
        assert result.cost_reserved_microusd == 20


async def test_verification_is_also_budgeted(tmp_path):
    step = replace(milestone(), verification_budget=CallBudget(tokens=1))
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step], limits=TaskLimits(max_tokens=0))
    result = await runner.run(task.task_id, [step])
    assert not result.completed
    assert result.calls_reserved == 1
    assert result.milestones["report"]["state"] == "executed"


@pytest.mark.parametrize(
    "answer",
    [VerificationResult(False, {}, "Missing report"), VerificationResult(True), {"done": True}],
)
async def test_unsupported_completion_never_advances(tmp_path, answer):
    async def reject(ctx):
        return answer

    async def never(ctx):
        pytest.fail("Unverified milestone advanced")

    steps = [replace(milestone(), verify=reject), Milestone("next", "Never", never, verify)]
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", steps)
    result = await runner.run(task.task_id, steps)
    assert result.status in ("invalid", "blocked")
    assert not result.completed
    assert result.next_milestone == "report"
    assert result.milestones["next"]["state"] == "ready"


async def test_reject_changed_contract(tmp_path):
    step = milestone()
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step])
    for changed in [
        replace(step, acceptance="Always pass"),
        replace(step, version="2"),
        replace(step, action_budget=CallBudget(tokens=100)),
    ]:
        with pytest.raises(ValueError, match="specification changed"):
            await runner.run(task.task_id, [changed])
    assert runner.store.load(task.task_id).calls_reserved == 0


async def test_concurrent_workers_cannot_duplicate_execution(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def wait(ctx):
        started.set()
        await release.wait()
        return await action(ctx)

    step = replace(milestone(), execute=wait)
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step])
    running = asyncio.create_task(runner.run(task.task_id, [step]))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(TaskBusyError):
            await runner_at(tmp_path).run(task.task_id, [step])
    finally:
        release.set()
        await running


async def test_provider_error_after_effect_requires_reconciliation(tmp_path):
    effects = []

    async def error(ctx):
        effects.append(ctx.operation_id)
        raise RuntimeError("Disconnected after acceptance")

    step = replace(milestone(), execute=error)
    runner = runner_at(tmp_path)
    task = runner.create("agent", "Write report", [step])
    for _ in range(2):
        result = await runner.run(task.task_id, [step])
        assert not result.completed
    assert len(effects) == 1


@pytest.mark.parametrize("control", ["pause", "stop"])
async def test_restart_does_not_override_operator_control(tmp_path, control):
    runner = runner_at(tmp_path)
    step = milestone()
    task = runner.create("agent", "Write report", [step])
    getattr(runner.manager, control)(task.session_id)
    result = await runner_at(tmp_path).run(task.task_id, [step])
    assert result.status == "blocked"
    assert result.calls_reserved == 0


async def test_agent_payload_cannot_change_task_budget_or_requirements(tmp_path):
    runner = runner_at(tmp_path)
    step = milestone()
    task = runner.create("agent", "Original goal", [step], limits=TaskLimits(max_calls=0))
    runner.manager.update_payload(
        task.session_id,
        {
            "limits": {"max_calls": 999},
            "goal": "Claim success",
            "status": "verified",
        },
    )
    result = await runner_at(tmp_path).run(task.task_id, [step])
    assert result.status == "blocked"
    assert result.goal == "Original goal"
    assert result.calls_reserved == 0


@pytest.mark.parametrize("value", [-1, True, 1.2])
def test_invalid_budgets(value):
    with pytest.raises(ValueError):
        CallBudget(tokens=value)
    with pytest.raises(ValueError):
        TaskLimits(max_calls=value)


def test_invalid_task_definitions(tmp_path):
    runner = runner_at(tmp_path)
    for steps in ([], [milestone(), milestone()], [replace(milestone(), acceptance="")]):
        with pytest.raises(ValueError):
            runner.create("agent", "goal", steps)
    with pytest.raises(ValueError):
        SqliteTaskStore(":memory:")
    with pytest.raises(KeyError):
        runner.store.load("missing")


# This is a real process death, not a simulated exception: no finally blocks run.
CRASH_WORKER = """
import asyncio, json, os, sys
from pathlib import Path
from agenthandler import *
root = Path(sys.argv[1])
async def execute(ctx):
    with (root / "receipt.json").open("x") as f:
        json.dump({"artifact": "report", "operation_id": ctx.operation_id}, f)
        f.flush()
        os.fsync(f.fileno())
    os._exit(77)
async def verify(ctx):
    return VerificationResult(True, {"receipt": ctx.output})
async def reconcile(ctx):
    return RecoveryResult("completed", json.loads((root / "receipt.json").read_text()))
step = Milestone("report", "Report exists", execute, verify, reconcile,
                 action_budget=CallBudget(tokens=10, cost_microusd=20))
runner = DurableTaskRunner(SessionManager(SqliteStore(str(root / "sessions.db"))),
                          SqliteTaskStore(str(root / "tasks.db")))
task = runner.create("agent", "Write report", [step])
(root / "task-id").write_text(task.task_id)
asyncio.run(runner.run(task.task_id, [step]))
"""


async def test_real_process_death_after_external_commit(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CRASH_WORKER,
        str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 77, stderr.decode()

    async def never(ctx):
        pytest.fail("External operation repeated after process death")

    async def reconcile(ctx):
        receipt = json.loads((tmp_path / "receipt.json").read_text())
        assert receipt["operation_id"] == ctx.operation_id
        return RecoveryResult("completed", receipt)

    step = Milestone(
        "report",
        "Report exists",
        never,
        verify,
        reconcile,
        action_budget=CallBudget(tokens=10, cost_microusd=20),
    )
    fresh = runner_at(tmp_path)
    task_id = (tmp_path / "task-id").read_text()
    pending = fresh.store.load(task_id)
    assert pending.milestones["report"]["state"] == "pending"
    assert pending.tokens_reserved == 10
    result = await fresh.run(task_id, [step])
    assert result.completed
    assert result.calls_reserved == 3
    assert result.tokens_reserved == 10
    assert result.cost_reserved_microusd == 20
