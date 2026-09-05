"""Regression coverage for orchestration and model routing."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agenthandler import ContextWindow, Policy, Supervisor
from agenthandler.a2a import A2ASupervisedEndpoint
from agenthandler.model_router import ModelRouter, RoutingRule
from agenthandler.pipeline import Pipeline
from agenthandler.session import SessionManager
from agenthandler.store import MemoryStore, SessionStatus


@pytest.mark.asyncio
async def test_a2a_cancel_stops_execution_and_cannot_be_overwritten():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def tool(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mgr = SessionManager(MemoryStore())
    sid = mgr.start("agent")
    endpoint = A2ASupervisedEndpoint(mgr, sid, tool_router=lambda _: tool)
    request = {"id": "request", "params": {"id": "task", "skill": "tool"}}
    sending = asyncio.create_task(endpoint.handle_send(request))
    try:
        await asyncio.wait_for(started.wait(), 1)
        duplicate = await endpoint.handle_send(request)
        assert duplicate["error"]["code"] == -32602
        response = await endpoint.handle_cancel({"params": {"id": "task"}})
        assert response["result"]["state"] == "canceled"
        result = await asyncio.wait_for(sending, 1)
        assert result["result"]["state"] == "canceled"
        assert cancelled.is_set()
        assert not endpoint._running_tasks
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_condition_approval_keeps_session_available_for_execution():
    mgr = SessionManager(MemoryStore())
    tool = AsyncMock(return_value="done")
    pipeline = Pipeline(mgr, "agent", policy_dict={"require_confirm": ["danger"]})
    pipeline.add_condition(lambda _: True, then_step=("danger", tool))
    result = await pipeline.run()
    assert not result.completed
    assert "awaiting approval" in result.error
    assert mgr.get_supervisor(result.session_id) is not None
    pending = mgr.approval_queue.list_pending(result.session_id)
    assert len(pending) == 1
    mgr.approval_queue.approve(pending[0].approval_id)
    assert (
        await mgr.get_supervisor(result.session_id).execute_approved(pending[0].approval_id, tool)
    ).succeeded


@pytest.mark.asyncio
async def test_iteration_limit_returns_pipeline_error_and_closes_owned_session():
    mgr = SessionManager(MemoryStore())
    pipeline = Pipeline(mgr, "agent", policy_dict={"max_iterations": 1})
    pipeline.add_step("one", AsyncMock(return_value="one"))
    pipeline.add_step("two", AsyncMock(return_value="two"))
    result = await pipeline.run()
    assert not result.completed
    assert len(result.steps) == 1
    assert result.error
    assert mgr.status(result.session_id).status == SessionStatus.STOPPED


@pytest.mark.asyncio
async def test_failed_pipeline_does_not_stop_borrowed_context_session():
    mgr = SessionManager(MemoryStore())
    sid = mgr.start("agent")
    pipeline = Pipeline(mgr, "agent").with_context(sid)
    pipeline.add_step("fail", AsyncMock(side_effect=RuntimeError("failed")))
    assert not (await pipeline.run()).completed
    assert mgr.get_supervisor(sid) is not None
    assert mgr.status(sid).status == SessionStatus.RUNNING


def test_appended_routing_rules_preserve_specific_priority():
    router = ModelRouter(rules=[RoutingRule("first", "first-model", keywords=["same"])])
    router.add_rule("second", "second-model", keywords=["same"])
    assert router.route("same").model == "first-model"
    router.add_rule("fallback", "fallback-model", match_default=True)
    router.add_rule("third", "third-model", keywords=["unique"])
    assert router.route("unique").model == "third-model"
    restored = ModelRouter.from_dict(router.to_dict())
    assert restored.route("same").name == "first"
    assert restored.route("unmatched").name == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_default_model_client_is_closed_on_success_and_error(monkeypatch, fails):
    client = SimpleNamespace(close=AsyncMock(), messages=SimpleNamespace(create=AsyncMock()))
    if fails:
        client.messages.create.side_effect = RuntimeError("provider failed")
    else:
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="ok")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        )
    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(AsyncAnthropic=lambda **_: client)
    )
    router = ModelRouter()
    if fails:
        with pytest.raises(RuntimeError):
            await router("prompt")
    else:
        assert await router("prompt") == "ok"
    client.close.assert_awaited_once()


@pytest.mark.parametrize("recent,summary", [(0, 10), (-1, 10), (10, 0)])
def test_invalid_context_window_limits_cannot_disable_compression(recent, summary):
    with pytest.raises(ValueError):
        ContextWindow(Supervisor(Policy()), max_recent_turns=recent, max_summary_tokens=summary)
