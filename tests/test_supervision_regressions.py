"""Regression tests for enforcement at tool execution boundaries."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from agenthandler import Policy, Supervisor
from agenthandler.budget import BudgetTracker
from agenthandler.circuit_breaker import CircuitBreaker, CircuitState
from agenthandler.errors import AgentHandlerError
from agenthandler.guardrails import GuardrailResult, IdempotencyGuard
from agenthandler.policy import ScopeRule, ToolScope
from agenthandler.sdk_adapters import _ensure_async
from agenthandler.session import SessionManager
from agenthandler.store import MemoryStore


@pytest.mark.asyncio
async def test_silence_checked_before_activity_is_updated():
    sv = Supervisor(Policy(silence_timeout=1))
    sv._last_activity -= 2
    tool = AsyncMock(return_value="unexpected")
    result = await sv.call("tool", tool)
    assert result.error.kind == "dead_man_switch"
    tool.assert_not_called()


@pytest.mark.asyncio
async def test_budget_breach_persists_and_blocks_further_calls():
    store = MemoryStore()
    mgr = SessionManager(store)
    sid = mgr.start("agent", {"token_budget": 2})
    sv = mgr.get_supervisor(sid)
    first = await sv.call("tool", AsyncMock(return_value={"tokens_used": 3}))
    assert first.succeeded and first.error.kind == "budget_exceeded"
    assert store.load_checkpoint(sid).tokens_used == 3
    tool = AsyncMock()
    result = await mgr.resume(sid).call("tool", tool)
    assert result.error.kind == "budget_exceeded"
    tool.assert_not_called()


@pytest.mark.parametrize("count", [-1, 1.5, float("nan"), "3", True])
def test_invalid_token_counts_cannot_refund_budget(count):
    tracker = BudgetTracker(token_limit=10)
    tracker.record_tokens(5)
    with pytest.raises(ValueError):
        tracker.record_tokens(count)
    assert tracker.snapshot().tokens_used == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [-1, "2", None, True])
async def test_malformed_tool_usage_returns_an_error(count):
    result = await Supervisor(Policy()).call("tool", AsyncMock(return_value={"tokens_used": count}))
    assert result.error.kind == "tool_error"


@pytest.mark.asyncio
async def test_cached_outputs_obey_redaction_post_guards_and_session_boundaries():
    cache = IdempotencyGuard(tracked_tools=["tool"])
    denied = False

    class Guard:
        name = "review"

        def check(self, tool_name, kwargs, output, context):
            return (
                GuardrailResult.block("changed policy", self.name)
                if denied
                else GuardrailResult.allow(self.name)
            )

    mgr = SessionManager(MemoryStore(), pre_guardrails=[cache], post_guardrails=[Guard()])
    first = mgr.get_supervisor(mgr.start("one", {"redact": ["email"]}))
    tool = AsyncMock(return_value={"text": "private@example.com", "tokens_used": 3})
    original = await first.call("tool", tool)
    cached = await first.call("tool", tool)
    assert original.succeeded and cached.succeeded
    assert "private@example.com" not in str(cached.output)
    assert first.budget().tokens_used == 3
    assert tool.await_count == 1
    denied = True
    assert not (await first.call("tool", tool)).succeeded
    denied = False
    second = mgr.get_supervisor(mgr.start("two"))
    other_tool = AsyncMock(return_value="other session")
    assert (await second.call("tool", other_tool)).output == "other session"
    other_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_one_recovery_probe_and_cancellation_reopens_breaker():
    sv = Supervisor(Policy(circuit_breaker_threshold=1, circuit_breaker_reset=0))
    await sv.call("tool", AsyncMock(side_effect=RuntimeError("offline")))
    started = asyncio.Event()

    async def probe():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(sv.call("tool", probe))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert (await sv.call("tool", probe)).error.kind == "circuit_open"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await sv.call("tool", AsyncMock(return_value="recovered"))).succeeded


def test_old_success_cannot_close_newly_tripped_circuit(monkeypatch):
    monkeypatch.setattr("agenthandler.circuit_breaker.time.monotonic", lambda: 0)
    cb = CircuitBreaker(threshold=1, reset_after=1)
    old = cb.check("tool")
    cb.record_failure(old)
    cb.record_success(old)
    assert cb.state == CircuitState.OPEN
    monkeypatch.setattr("agenthandler.circuit_breaker.time.monotonic", lambda: 1)
    probe = cb.check("tool")
    with pytest.raises(AgentHandlerError):
        cb.check("tool")
    cb.record_success(probe)
    assert cb.state == CircuitState.CLOSED


def test_sync_timeout_returns_while_worker_is_still_blocked():
    release, finished = threading.Event(), threading.Event()

    def tool():
        try:
            release.wait(2)
        finally:
            finished.set()

    try:
        result = Supervisor(Policy(tool_timeout=0.02)).call_sync("tool", tool)
        assert result.error.kind == "timeout"
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.asyncio
async def test_sdk_sync_wrapper_keeps_event_loop_responsive():
    release = threading.Event()
    try:
        sv = Supervisor(Policy(tool_timeout=0.02))
        result = await sv.call("tool", _ensure_async(lambda: release.wait(2)))
        assert result.error.kind == "timeout"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_request_deadline_bounds_a_running_tool():
    result = await Supervisor(Policy(tool_timeout=2, request_timeout=0.02)).call(
        "tool", AsyncMock(side_effect=lambda: None)
    )
    assert result.succeeded
    sv = Supervisor(Policy(tool_timeout=2, request_timeout=0.02))
    result = await sv.call("tool", lambda: asyncio.sleep(0.2))
    assert result.error.kind == "timeout"


@pytest.mark.asyncio
async def test_scope_rules_apply_to_default_arguments():
    called = False

    async def tool(path="/private"):
        nonlocal called
        called = True

    scope = ToolScope("tool", [ScopeRule("path", "allow", ["/safe/*"])])
    result = await Supervisor(Policy(tool_scopes=[scope])).call("tool", tool)
    assert result.error.kind == "scope_denied"
    assert not called


def test_misspelled_scope_constraint_is_rejected():
    with pytest.raises(ValueError, match="constraint"):
        ScopeRule("path", "alow", ["/safe/*"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "invalid"])
async def test_guardrail_failures_do_not_allow_tool_execution(failure):
    class Guard:
        name = "safety"

        async def check_async(self, tool_name, kwargs, context):
            if failure == "error":
                raise RuntimeError("offline")
            if failure == "timeout":
                await asyncio.sleep(1)
            return None

    tool = AsyncMock()
    sv = Supervisor(Policy(tool_timeout=0.02), pre_guardrails=[Guard()])
    assert (await sv.call("tool", tool)).error.kind == "policy_denied"
    tool.assert_not_called()


@pytest.mark.parametrize(
    "url", ["https://blocked.test:443/a", "https://user@blocked.test/a", "HTTPS://BLOCKED.TEST./a"]
)
def test_url_blocklist_checks_hostname_in_nested_arguments(url):
    from agenthandler.guardrails import UrlGuard

    result = UrlGuard(blocklist=["blocked.test"]).check("tool", {"items": [{"url": url}]}, {})
    assert not result.allowed
