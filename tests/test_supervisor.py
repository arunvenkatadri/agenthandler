"""Tests for agenthandler.supervisor — Supervisor and SupervisedResult."""

import asyncio

import pytest

from agenthandler.audit import AuditLog, MemoryAuditSink
from agenthandler.errors import AgentHandlerError
from agenthandler.policy import Policy
from agenthandler.supervisor import SupervisedResult, Supervisor

# ---------------------------------------------------------------------------
# Helper tool functions
# ---------------------------------------------------------------------------


async def good_tool(query: str = "test") -> str:
    return f"result for {query}"


async def failing_tool(**kwargs) -> str:
    raise RuntimeError("tool exploded")


async def slow_tool(delay: float = 10.0) -> str:
    await asyncio.sleep(delay)
    return "done"


async def token_reporting_tool(**kwargs) -> dict:
    return {"output": "data", "tokens_used": 500}


def sync_tool(x: int = 1) -> int:
    return x * 2


# ---------------------------------------------------------------------------
# SupervisedResult
# ---------------------------------------------------------------------------


class TestSupervisedResult:
    def test_user_message_on_success(self):
        r = SupervisedResult(output="hello", succeeded=True)
        assert r.user_message() == "hello"

    def test_user_message_on_error(self):
        err = AgentHandlerError.timeout(5000)
        r = SupervisedResult(error=err, succeeded=False)
        msg = r.user_message()
        assert "5000" in msg

    def test_user_message_no_output_no_error(self):
        r = SupervisedResult(succeeded=True)
        assert "No output" in r.user_message()

    def test_user_message_none_output(self):
        r = SupervisedResult(output=None, succeeded=True)
        assert "No output" in r.user_message()


# ---------------------------------------------------------------------------
# Supervisor — successful calls
# ---------------------------------------------------------------------------


class TestSupervisorSuccessfulCall:
    @pytest.mark.asyncio
    async def test_successful_async_tool_call(self):
        sv = Supervisor(Policy())
        result = await sv.call("search", good_tool, query="python")
        assert result.succeeded is True
        assert result.output == "result for python"
        assert result.error is None
        assert result.tool_name == "search"

    @pytest.mark.asyncio
    async def test_duration_is_recorded(self):
        sv = Supervisor(Policy())
        result = await sv.call("search", good_tool)
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_budget_snapshot_is_attached(self):
        sv = Supervisor(Policy())
        result = await sv.call("search", good_tool)
        assert result.budget is not None
        assert result.budget.iterations == 0  # call doesn't increment iterations


# ---------------------------------------------------------------------------
# Supervisor — error handling
# ---------------------------------------------------------------------------


class TestSupervisorErrors:
    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_result(self):
        sv = Supervisor(Policy())
        result = await sv.call("bad_tool", failing_tool)
        assert result.succeeded is False
        assert result.error is not None
        assert result.error.kind == "tool_error"
        assert "exploded" in str(result.error)

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_timeout_error(self):
        policy = Policy(tool_timeout=0.05)
        sv = Supervisor(policy)
        result = await sv.call("slow", slow_tool, delay=10.0)
        assert result.succeeded is False
        assert result.error is not None
        assert result.error.kind == "timeout"


# ---------------------------------------------------------------------------
# Supervisor — budget tracking
# ---------------------------------------------------------------------------


class TestSupervisorBudgetTracking:
    @pytest.mark.asyncio
    async def test_record_tokens_updates_budget(self):
        policy = Policy(token_budget=10000)
        sv = Supervisor(policy)
        sv.record_tokens(500)
        snap = sv.budget()
        assert snap.tokens_used == 500

    @pytest.mark.asyncio
    async def test_record_tokens_raises_when_over_budget(self):
        policy = Policy(token_budget=1000)
        sv = Supervisor(policy)
        sv.record_tokens(800)
        with pytest.raises(AgentHandlerError) as exc_info:
            sv.record_tokens(300)
        assert exc_info.value.kind == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_auto_token_tracking_from_tool_output(self):
        policy = Policy(token_budget=10000)
        sv = Supervisor(policy)
        result = await sv.call("tok_tool", token_reporting_tool)
        assert result.succeeded is True
        snap = sv.budget()
        assert snap.tokens_used == 500

    @pytest.mark.asyncio
    async def test_budget_across_multiple_calls(self):
        policy = Policy(token_budget=10000)
        sv = Supervisor(policy)
        await sv.call("tok_tool", token_reporting_tool)
        await sv.call("tok_tool", token_reporting_tool)
        snap = sv.budget()
        assert snap.tokens_used == 1000


# ---------------------------------------------------------------------------
# Supervisor — circuit breaker
# ---------------------------------------------------------------------------


class TestSupervisorCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers_after_consecutive_failures(self):
        policy = Policy(circuit_breaker_threshold=2)
        sv = Supervisor(policy)
        # Two failures should trip the circuit
        await sv.call("flaky", failing_tool)
        await sv.call("flaky", failing_tool)
        # Third call should be blocked by circuit breaker
        result = await sv.call("flaky", failing_tool)
        assert result.succeeded is False
        assert result.error.kind == "circuit_open"

    @pytest.mark.asyncio
    async def test_reset_circuit_breaker_closes_it(self):
        sv = Supervisor(Policy(circuit_breaker_threshold=2))
        sv.reset_circuit_breaker("flaky")  # unknown tool: nothing to reset
        await sv.call("flaky", failing_tool)
        await sv.call("flaky", failing_tool)
        assert sv.circuit_breaker_states()["flaky"] == "open"
        sv.reset_circuit_breaker("flaky")
        assert sv.circuit_breaker_states()["flaky"] == "closed"
        assert (await sv.call("flaky", good_tool)).succeeded is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_is_per_tool(self):
        policy = Policy(circuit_breaker_threshold=2)
        sv = Supervisor(policy)
        await sv.call("flaky", failing_tool)
        await sv.call("flaky", failing_tool)
        # Different tool should still work
        result = await sv.call("good", good_tool)
        assert result.succeeded is True

    @pytest.mark.asyncio
    async def test_restored_open_circuit_keeps_its_age(self, monkeypatch):
        from agenthandler.store import Checkpoint, SessionStatus

        policy = Policy(circuit_breaker_threshold=1, circuit_breaker_reset=60.0)
        sv = Supervisor(policy)
        await sv.call("flaky", failing_tool)
        data = sv.to_checkpoint_data()
        assert data["circuit_breaker_states"]["flaky"]["state"] == "open"
        checkpoint = Checkpoint(
            session_id="s",
            agent_id="a",
            status=SessionStatus.RUNNING,
            circuit_breaker_states=data["circuit_breaker_states"],
            policy_dict=policy.to_dict(),
        )
        wall = __import__("time").time()
        monkeypatch.setattr("agenthandler.supervisor.time.time", lambda: wall + 61.0)
        restored = Supervisor.restore_from_checkpoint(checkpoint)
        # The reset window elapsed while no supervisor existed: a test call is allowed.
        result = await restored.call("flaky", good_tool)
        assert result.succeeded is True

    @pytest.mark.asyncio
    async def test_restored_open_circuit_without_age_stays_open(self):
        from agenthandler.store import Checkpoint, SessionStatus

        checkpoint = Checkpoint(
            session_id="s",
            agent_id="a",
            status=SessionStatus.RUNNING,
            circuit_breaker_states={"flaky": {"state": "open", "consecutive_failures": 1}},
            policy_dict=Policy(circuit_breaker_threshold=1).to_dict(),
        )
        restored = Supervisor.restore_from_checkpoint(checkpoint)
        result = await restored.call("flaky", good_tool)
        assert result.error.kind == "circuit_open"


class TestSupervisorPerToolTimeouts:
    @pytest.mark.asyncio
    async def test_tool_timeouts_override_the_default_per_tool(self):
        policy = Policy(tool_timeout=0.05, tool_timeouts={"slow": 1.0})
        sv = Supervisor(policy)
        allowed = await sv.call("slow", slow_tool, delay=0.1)
        assert allowed.succeeded is True
        cut_off = await sv.call("other", slow_tool, delay=0.1)
        assert cut_off.error.kind == "timeout"

    def test_tool_timeouts_round_trip_through_dict(self):
        policy = Policy.from_dict({"tool_timeouts": {"model": 300}})
        assert policy.tool_timeouts == {"model": 300.0}
        assert Policy.from_dict(policy.to_dict()).tool_timeouts == {"model": 300.0}
        assert "tool_timeouts" not in Policy().to_dict()


# ---------------------------------------------------------------------------
# Supervisor — iterations
# ---------------------------------------------------------------------------


class TestSupervisorIterations:
    def test_record_iteration_increments(self):
        sv = Supervisor(Policy(max_iterations=5))
        assert sv.record_iteration() == 1
        assert sv.record_iteration() == 2

    def test_record_iteration_raises_when_over_limit(self):
        sv = Supervisor(Policy(max_iterations=2))
        sv.record_iteration()
        sv.record_iteration()
        with pytest.raises(AgentHandlerError) as exc_info:
            sv.record_iteration()
        assert exc_info.value.kind == "max_iterations"


# ---------------------------------------------------------------------------
# Supervisor — request_timeout
# ---------------------------------------------------------------------------


class TestSupervisorRequestTimeout:
    @pytest.mark.asyncio
    async def test_request_timeout_triggers(self):
        # Set request_timeout generous enough that sleep imprecision doesn't matter
        policy = Policy(request_timeout=0.05, tool_timeout=5.0)
        sv = Supervisor(policy)
        # Wait well past the request timeout
        await asyncio.sleep(0.2)
        result = await sv.call("search", good_tool)
        assert result.succeeded is False
        assert result.error.kind == "timeout"


# ---------------------------------------------------------------------------
# Supervisor — require_confirm
# ---------------------------------------------------------------------------


class TestSupervisorRequireConfirm:
    @pytest.mark.asyncio
    async def test_require_confirm_blocks_tool(self):
        policy = Policy(require_confirm=["dangerous_tool"])
        sv = Supervisor(policy)
        result = await sv.call("dangerous_tool", good_tool)
        assert result.succeeded is False
        assert result.error.kind == "policy_denied"
        assert "confirmation" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_non_confirmed_tool_passes(self):
        policy = Policy(require_confirm=["dangerous_tool"])
        sv = Supervisor(policy)
        result = await sv.call("safe_tool", good_tool)
        assert result.succeeded is True


# ---------------------------------------------------------------------------
# Supervisor — finish
# ---------------------------------------------------------------------------


class TestSupervisorFinish:
    def test_finish_returns_budget_snapshot(self):
        sv = Supervisor(Policy(token_budget=10000))
        sv.record_tokens(250)
        sv.record_iteration()
        snap = sv.finish()
        assert snap.tokens_used == 250
        assert snap.iterations == 1
        assert snap.token_limit == 10000

    def test_finish_records_audit_entry(self):
        sink = MemoryAuditSink()
        audit = AuditLog("req-fin", sinks=[sink])
        sv = Supervisor(Policy(), audit=audit)
        sv.finish()
        # Should have at least REQUEST_START and REQUEST_END
        phases = [e.phase for e in sink.entries]
        assert "request_start" in phases
        assert "request_end" in phases


# ---------------------------------------------------------------------------
# Supervisor — with_defaults
# ---------------------------------------------------------------------------


class TestSupervisorWithDefaults:
    @pytest.mark.asyncio
    async def test_with_defaults_creates_working_supervisor(self):
        sv = Supervisor.with_defaults()
        result = await sv.call("search", good_tool, query="hello")
        assert result.succeeded is True
        assert result.output == "result for hello"


# ---------------------------------------------------------------------------
# Supervisor — call_sync
# ---------------------------------------------------------------------------


class TestSupervisorCallSync:
    def test_call_sync_works(self):
        sv = Supervisor(Policy())
        result = sv.call_sync("math", sync_tool, x=5)
        assert result.succeeded is True
        assert result.output == 10

    def test_call_sync_handles_error(self):
        def broken():
            raise ValueError("bad input")

        sv = Supervisor(Policy())
        result = sv.call_sync("broken", broken)
        assert result.succeeded is False
        assert result.error.kind == "tool_error"
