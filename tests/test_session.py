"""Tests for agenthandler.session — SessionManager lifecycle and security invariants."""

import pytest

from agenthandler.errors import AgentHandlerError
from agenthandler.session import SessionManager
from agenthandler.store import MAX_PAYLOAD_BYTES, MemoryStore, SessionStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def good_tool(query: str = "test") -> str:
    return f"result for {query}"


POLICY = {"max_iterations": 5, "tool_timeout": 10, "token_budget": 10000}


# ---------------------------------------------------------------------------
# Lifecycle: start → pause → resume → stop
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_start_creates_session(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, {"task": "research"})
        assert isinstance(sid, str)
        cp = mgr.status(sid)
        assert cp is not None
        assert cp.agent_id == "agent-1"
        assert cp.status == SessionStatus.RUNNING
        assert cp.payload == {"task": "research"}

    def test_start_returns_unique_ids(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        ids = {mgr.start("agent-1", POLICY) for _ in range(50)}
        assert len(ids) == 50

    def test_pause_sets_status(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.pause(sid)
        cp = mgr.status(sid)
        assert cp.status == SessionStatus.PAUSED

    @pytest.mark.asyncio
    async def test_pause_blocks_tool_calls(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        sv = mgr.get_supervisor(sid)
        mgr.pause(sid)
        result = await sv.call("search", good_tool, query="python")
        assert result.succeeded is False
        assert result.error.kind == "agent_paused"

    def test_resume_clears_pause(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.pause(sid)
        sv = mgr.resume(sid)
        assert sv.paused is False
        cp = mgr.status(sid)
        assert cp.status == SessionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resume_restores_budget_state(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        sv = mgr.get_supervisor(sid)
        sv.record_tokens(500)
        sv.record_iteration()
        mgr.pause(sid)
        sv2 = mgr.resume(sid)
        snap = sv2.budget()
        assert snap.tokens_used == 500
        assert snap.iterations == 1

    def test_stop_marks_stopped(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.stop(sid)
        cp = mgr.status(sid)
        assert cp.status == SessionStatus.STOPPED

    def test_stop_removes_supervisor_from_memory(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.stop(sid)
        assert mgr.get_supervisor(sid) is None

    def test_list_sessions(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        s1 = mgr.start("agent-1", POLICY)
        s2 = mgr.start("agent-2", POLICY)
        sessions = mgr.list_sessions()
        ids = {s.session_id for s in sessions}
        assert s1 in ids
        assert s2 in ids

    def test_status_missing_returns_none(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        assert mgr.status("nonexistent") is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestSessionErrors:
    def test_pause_nonexistent_raises(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr.pause("nope")
        assert exc_info.value.kind == "session_not_found"

    def test_resume_nonexistent_raises(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr.resume("nope")
        assert exc_info.value.kind == "session_not_found"

    def test_stop_nonexistent_raises(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr.stop("nope")
        assert exc_info.value.kind == "session_not_found"


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_recover_after_simulated_crash(self):
        """Create session, use supervisor, discard manager, resume from same store."""
        store = MemoryStore()

        # Phase 1: create and use
        mgr1 = SessionManager(store)
        sid = mgr1.start("agent-1", POLICY)
        sv = mgr1.get_supervisor(sid)
        sv.record_tokens(750)
        sv.record_iteration()
        sv.record_iteration()
        await sv.call("search", good_tool, query="test")

        # "Crash" — discard the manager entirely
        del mgr1

        # Phase 2: new manager, same store
        mgr2 = SessionManager(store)
        sv2 = mgr2.resume(sid)
        snap = sv2.budget()
        assert snap.tokens_used == 750
        assert snap.iterations == 2
        # Can still make calls
        result = await sv2.call("search", good_tool, query="recovered")
        assert result.succeeded is True
        assert result.output == "result for recovered"

    def test_recover_preserves_payload(self):
        store = MemoryStore()
        mgr1 = SessionManager(store)
        sid = mgr1.start("agent-1", POLICY, {"step": 3, "data": [1, 2, 3]})
        del mgr1
        mgr2 = SessionManager(store)
        cp = mgr2.status(sid)
        assert cp.payload == {"step": 3, "data": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Payload management
# ---------------------------------------------------------------------------


class TestPayload:
    def test_update_payload(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, {"step": 1})
        mgr.update_payload(sid, {"step": 2, "results": ["a", "b"]})
        cp = mgr.status(sid)
        assert cp.payload == {"step": 2, "results": ["a", "b"]}

    def test_update_payload_nonexistent_raises(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        with pytest.raises(AgentHandlerError):
            mgr.update_payload("nope", {})

    def test_oversized_payload_rejected_on_start(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        huge = {"data": "x" * (MAX_PAYLOAD_BYTES + 1)}
        with pytest.raises(ValueError, match="maximum size"):
            mgr.start("agent-1", POLICY, huge)

    def test_oversized_payload_rejected_on_update(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        huge = {"data": "x" * (MAX_PAYLOAD_BYTES + 1)}
        with pytest.raises(ValueError, match="maximum size"):
            mgr.update_payload(sid, huge)


# ---------------------------------------------------------------------------
# Audit entries
# ---------------------------------------------------------------------------


class TestAuditEntries:
    def test_get_audit_entries(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        entries = mgr.get_audit_entries(sid)
        # At minimum, REQUEST_START should be recorded
        assert len(entries) >= 1
        assert any(e.get("phase") == "request_start" for e in entries)

    def test_get_audit_entries_missing_session(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        assert mgr.get_audit_entries("nope") == []

    def test_audit_persisted_on_pause(self):
        """Audit entries should be persisted to the store when pausing."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.pause(sid)
        cp = store.load_checkpoint(sid)
        assert len(cp.audit_log) > 0
        assert any(e.get("phase") == "request_start" for e in cp.audit_log)

    def test_audit_persisted_on_stop(self):
        """Audit entries should be persisted to the store when stopping."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.stop(sid)
        cp = store.load_checkpoint(sid)
        assert len(cp.audit_log) > 0

    def test_audit_survives_crash(self):
        """After pause (persists audit), crash, and resume — audit is recoverable."""
        store = MemoryStore()
        mgr1 = SessionManager(store)
        sid = mgr1.start("agent-1", POLICY)
        mgr1.pause(sid)
        del mgr1

        mgr2 = SessionManager(store)
        entries = mgr2.get_audit_entries(sid)
        assert len(entries) > 0
        assert any(e.get("phase") == "request_start" for e in entries)


# ---------------------------------------------------------------------------
# Security: Policy immutability
# ---------------------------------------------------------------------------


class TestPolicyImmutability:
    def test_resume_uses_original_policy(self):
        """Even if checkpoint policy_dict is tampered with, resume uses the original."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start(
            "agent-1",
            {
                "max_iterations": 3,
                "token_budget": 1000,
                "require_confirm": ["dangerous_tool"],
            },
        )

        # Simulate an attacker tampering with the stored checkpoint's policy
        cp = store.load_checkpoint(sid)
        cp.policy_dict = {
            "max_iterations": 9999,
            "token_budget": 999999999,
            "require_confirm": [],  # attacker removed confirmation gate!
        }
        store.save_checkpoint(cp)

        # Resume should use the ORIGINAL policy, not the tampered one
        sv = mgr.resume(sid)
        assert sv.policy.max_iterations == 3
        assert sv.policy.token_budget == 1000
        assert "dangerous_tool" in sv.policy.require_confirm

    def test_resume_after_crash_uses_stored_original_policy(self):
        """After a process restart, resume uses the checkpoint's policy_dict
        which was set at start() and never overwritten by auto-checkpoint."""
        store = MemoryStore()
        mgr1 = SessionManager(store)
        sid = mgr1.start(
            "agent-1",
            {
                "max_iterations": 3,
                "require_confirm": ["dangerous_tool"],
            },
        )
        del mgr1

        # New manager — no in-memory cache of original policy
        mgr2 = SessionManager(store)
        sv = mgr2.resume(sid)
        assert sv.policy.max_iterations == 3
        assert "dangerous_tool" in sv.policy.require_confirm

    @pytest.mark.asyncio
    async def test_auto_checkpoint_does_not_overwrite_policy(self):
        """Auto-checkpoint after tool calls should not change the policy in the store."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start(
            "agent-1",
            {
                "max_iterations": 5,
                "require_confirm": ["delete_file"],
            },
        )
        sv = mgr.get_supervisor(sid)
        await sv.call("search", good_tool, query="test")

        # Check the stored checkpoint still has the original policy
        cp = store.load_checkpoint(sid)
        assert cp.policy_dict["max_iterations"] == 5
        assert "delete_file" in cp.policy_dict["require_confirm"]

    @pytest.mark.asyncio
    async def test_auto_checkpoint_preserves_payload(self):
        """Auto-checkpoint should not wipe the payload."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, {"important": "data"})
        sv = mgr.get_supervisor(sid)
        await sv.call("search", good_tool, query="test")

        cp = store.load_checkpoint(sid)
        assert cp.payload == {"important": "data"}


# ---------------------------------------------------------------------------
# Session ID entropy
# ---------------------------------------------------------------------------


class TestSessionIdSecurity:
    def test_session_id_length(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        assert len(sid) == 32  # 128 bits of entropy

    def test_session_ids_not_sequential(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        ids = [mgr.start("agent-1", POLICY) for _ in range(10)]
        # No common prefix — crypto random
        prefixes = {s[:8] for s in ids}
        assert len(prefixes) == 10


# ---------------------------------------------------------------------------
# Stateless sessions
# ---------------------------------------------------------------------------


class TestStatelessSessions:
    @pytest.mark.asyncio
    async def test_stateless_session_works(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, stateless=True)
        sv = mgr.get_supervisor(sid)
        result = await sv.call("search", good_tool, query="test")
        assert result.succeeded is True

    def test_stateless_flag_stored_in_checkpoint(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, stateless=True)
        cp = mgr.status(sid)
        assert cp.stateless is True

    def test_stateful_flag_default(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        cp = mgr.status(sid)
        assert cp.stateless is False

    def test_stateless_pause_and_unpause_in_memory(self):
        """Stateless sessions can be paused/resumed while still in memory."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, stateless=True)
        mgr.pause(sid)
        sv = mgr.resume(sid)
        assert sv.paused is False

    def test_stateless_cannot_resume_after_crash(self):
        """Stateless sessions cannot be recovered after the manager is gone."""
        store = MemoryStore()
        mgr1 = SessionManager(store)
        sid = mgr1.start("agent-1", POLICY, stateless=True)
        del mgr1

        mgr2 = SessionManager(store)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr2.resume(sid)
        assert exc_info.value.kind == "session_not_found"

    def test_stateless_stop_works(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, stateless=True)
        mgr.stop(sid)
        cp = mgr.status(sid)
        assert cp.status == SessionStatus.STOPPED

    @pytest.mark.asyncio
    async def test_stateless_no_auto_checkpoint(self):
        """Stateless sessions should not update checkpoint on every tool call."""
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY, stateless=True)
        sv = mgr.get_supervisor(sid)
        sv.record_tokens(500)
        await sv.call("search", good_tool, query="test")
        # The checkpoint should still have 0 tokens (no auto-checkpoint)
        cp = mgr.status(sid)
        assert cp.tokens_used == 0
        assert cp.iterations == 0


# ---------------------------------------------------------------------------
# Crash loop protection (max_resumes)
# ---------------------------------------------------------------------------


class TestCrashLoopProtection:
    def test_default_max_resumes_is_3(self):
        """Policy defaults to max_resumes=3."""
        from agenthandler.policy import Policy

        p = Policy()
        assert p.max_resumes == 3

    def test_resume_increments_count(self):
        store = MemoryStore()
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", POLICY)
        mgr.pause(sid)
        mgr.resume(sid)
        cp = mgr.status(sid)
        assert cp.resume_count == 1

    def test_resume_count_persists_across_crashes(self):
        store = MemoryStore()
        mgr1 = SessionManager(store)
        sid = mgr1.start("agent-1", POLICY)
        mgr1.pause(sid)
        mgr1.resume(sid)
        del mgr1

        mgr2 = SessionManager(store)
        mgr2.pause(sid)
        mgr2.resume(sid)
        cp = mgr2.status(sid)
        assert cp.resume_count == 2

    def test_max_resumes_exceeded_marks_failed(self):
        """After max_resumes, session is marked FAILED and resume raises."""
        store = MemoryStore()
        policy = {"max_iterations": 5, "max_resumes": 2}
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", policy)

        # Resume 1
        mgr.pause(sid)
        mgr.resume(sid)

        # Resume 2
        mgr.pause(sid)
        mgr.resume(sid)

        # Resume 3 — should fail (limit is 2)
        mgr.pause(sid)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr.resume(sid)
        assert exc_info.value.kind == "max_resumes_exceeded"

        # Session is marked as failed
        cp = mgr.status(sid)
        assert cp.status == SessionStatus.FAILED

    def test_crash_loop_after_repeated_crashes(self):
        """Simulate an agent that keeps crashing and being resumed."""
        store = MemoryStore()
        policy = {"max_iterations": 5, "max_resumes": 2}

        # Process 1: start, crash
        mgr1 = SessionManager(store)
        sid = mgr1.start("crashy-agent", policy)
        del mgr1

        # Process 2: resume, crash
        mgr2 = SessionManager(store)
        mgr2.resume(sid)
        del mgr2

        # Process 3: resume, crash
        mgr3 = SessionManager(store)
        mgr3.resume(sid)
        del mgr3

        # Process 4: resume should fail — crash loop detected
        mgr4 = SessionManager(store)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr4.resume(sid)
        assert exc_info.value.kind == "max_resumes_exceeded"
        assert "crash loop" in str(exc_info.value).lower()

    def test_custom_max_resumes(self):
        store = MemoryStore()
        policy = {"max_iterations": 5, "max_resumes": 10}
        mgr = SessionManager(store)
        sid = mgr.start("agent-1", policy)
        # Should allow 10 resumes
        for _ in range(10):
            mgr.pause(sid)
            mgr.resume(sid)
        # 11th should fail
        mgr.pause(sid)
        with pytest.raises(AgentHandlerError) as exc_info:
            mgr.resume(sid)
        assert exc_info.value.kind == "max_resumes_exceeded"


@pytest.mark.asyncio
async def test_sqlite_security_metadata_survives_tools_and_process_restarts(tmp_path):
    from agenthandler.store import SqliteStore

    path = str(tmp_path / "state.db")
    mgr = SessionManager(SqliteStore(path))
    sid = mgr.start("agent", {"max_resumes": 1})
    checksum = mgr.status(sid).policy_checksum
    sv = mgr.resume(sid)
    await sv.call("search", good_tool, query="test")
    cp = SqliteStore(path).load_checkpoint(sid)
    assert cp.resume_count == 1
    assert cp.policy_checksum == checksum
    with pytest.raises(AgentHandlerError, match="resum"):
        SessionManager(SqliteStore(path)).resume(sid)
    assert SqliteStore(path).load_checkpoint(sid).failure_reason


@pytest.mark.asyncio
async def test_sqlite_tampering_rejected_after_tool_checkpoint_and_restart(tmp_path):
    from agenthandler.store import SqliteStore

    store = SqliteStore(str(tmp_path / "state.db"))
    mgr = SessionManager(store)
    sid = mgr.start("agent", {"max_iterations": 2})
    await mgr.get_supervisor(sid).call("search", good_tool)
    cp = store.load_checkpoint(sid)
    cp.policy_dict["max_iterations"] = 999
    store.save_checkpoint(cp)
    with pytest.raises(AgentHandlerError) as exc:
        SessionManager(store).resume(sid)
    assert exc.value.kind == "policy_tampered"


@pytest.mark.asyncio
async def test_stopped_and_replaced_supervisors_cannot_execute():
    mgr = SessionManager(MemoryStore())
    sid = mgr.start("agent")
    old = mgr.get_supervisor(sid)
    replacement = mgr.resume(sid)
    assert (await old.call("search", good_tool)).error.kind == "agent_paused"
    mgr.stop(sid)
    assert (await replacement.call("search", good_tool)).error.kind == "agent_paused"
    assert mgr.status(sid).status == SessionStatus.STOPPED


def test_pause_resume_does_not_duplicate_or_drop_audit_entries():
    mgr = SessionManager(MemoryStore())
    sid = mgr.start("agent")
    initial = mgr.get_audit_entries(sid)
    mgr.pause(sid)
    mgr.pause(sid)
    assert mgr.get_audit_entries(sid) == initial
    mgr.resume(sid)
    after_resume = mgr.get_audit_entries(sid)
    assert after_resume[: len(initial)] == initial
    mgr.pause(sid)
    assert mgr.get_audit_entries(sid) == after_resume
    mgr.stop(sid)
    assert mgr.get_audit_entries(sid)[: len(after_resume)] == after_resume


@pytest.mark.asyncio
async def test_stateless_session_never_writes_to_disk_even_after_resume(tmp_path):
    from agenthandler.store import SqliteStore

    store = SqliteStore(str(tmp_path / "state.db"))
    mgr = SessionManager(store)
    sid = mgr.start("agent", stateless=True, payload={"private": "data"})
    mgr.pause(sid)
    sv = mgr.resume(sid)
    await sv.call("search", good_tool)
    mgr.update_payload(sid, {"private": "updated"})
    mgr.stop(sid)
    assert store.list_sessions() == []
    assert mgr.status(sid).status == SessionStatus.STOPPED


@pytest.mark.parametrize("operation", ["pause", "stop", "resume"])
async def test_external_manager_control_fences_inflight_supervisor_checkpoint(tmp_path, operation):
    from agenthandler.store import SqliteStore

    path = str(tmp_path / "state.db")
    owner = SessionManager(SqliteStore(path))
    control = SessionManager(SqliteStore(path))
    sid = owner.start("agent", POLICY)
    old = owner.get_supervisor(sid)
    old.record_tokens(2)
    expected = None

    async def inflight():
        nonlocal expected
        if operation == "resume":
            replacement = control.resume(sid)
            assert replacement.resume_count == 1
            replacement.record_tokens(5)
        else:
            getattr(control, operation)(sid)
        expected = control.status(sid)
        # An old SDK continuation must not overwrite the replacement's counters.
        old.record_tokens(10)
        return "external effect completed"

    assert (await old.call("inflight", inflight)).succeeded
    assert owner.status(sid) == expected
    assert old.resume_count == 0
    assert old.paused
    assert not old.supports_atomic_checkpoints
    assert (await old.call("again", good_tool)).error.kind == "agent_paused"


class LegacyStore:
    def __init__(self, inner=None):
        self.inner = inner or MemoryStore()
        self.saves = 0

    def save_checkpoint(self, checkpoint):
        self.saves += 1
        self.inner.save_checkpoint(checkpoint)

    def load_checkpoint(self, session_id):
        return self.inner.load_checkpoint(session_id)

    def list_sessions(self):
        return self.inner.list_sessions()

    def delete_session(self, session_id):
        return self.inner.delete_session(session_id)


def test_legacy_store_rejects_persistent_start_before_creating_anything(monkeypatch):
    def unexpected_id():
        pytest.fail("Unsupported persistence must be rejected before allocating a session")

    monkeypatch.setattr("agenthandler.session.new_session_id", unexpected_id)
    store = LegacyStore()
    manager = SessionManager(store)
    with pytest.raises(ValueError, match="AtomicCheckpointStore"):
        manager.start("agent", POLICY)
    assert store.saves == 0
    assert store.list_sessions() == []
    assert manager._supervisors == {}


def test_legacy_store_rejects_persistent_resume_without_mutating_checkpoint():
    inner = MemoryStore()
    owner = SessionManager(inner)
    sid = owner.start("agent", POLICY)
    original = owner.status(sid)
    store = LegacyStore(inner)
    fresh = SessionManager(store)
    with pytest.raises(ValueError, match="AtomicCheckpointStore"):
        fresh.resume(sid)
    assert store.saves == 0
    assert fresh.status(sid) == original
    assert fresh.get_supervisor(sid) is None


async def test_legacy_store_still_supports_explicit_stateless_sessions():
    store = LegacyStore()
    manager = SessionManager(store)
    sid = manager.start("agent", POLICY, stateless=True)
    supervisor = manager.get_supervisor(sid)
    supervisor.record_tokens(1)
    manager.pause(sid)
    resumed = manager.resume(sid)
    assert resumed.budget().tokens_used == 1
    assert (await resumed.call("search", good_tool)).succeeded
    manager.stop(sid)
    assert manager.status(sid).status == SessionStatus.STOPPED
    assert store.saves == 0
    assert store.list_sessions() == []


def test_direct_supervisor_rejects_legacy_store_without_unsafe_persistence():
    from agenthandler import Policy, Supervisor

    store = LegacyStore()
    with pytest.raises(ValueError, match="atomic"):
        Supervisor(Policy(), store=store, session_id="legacy")
    assert store.saves == 0
    assert store.list_sessions() == []


@pytest.mark.parametrize("operation", ["pause", "stop", "resume"])
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_stale_manager_preserves_new_generation_state(tmp_path, operation, kind):
    from agenthandler import SqliteStore

    store = MemoryStore() if kind == "memory" else SqliteStore(str(tmp_path / "sessions.db"))
    original = SessionManager(store)
    newer = SessionManager(store)
    sid = original.start("agent", POLICY)
    old = original.get_supervisor(sid)
    active = newer.resume(sid)
    active.record_tokens(7)
    active.record_iteration()
    newer.pause(sid)  # Flush the newer generation's audit.
    before = newer.status(sid)
    getattr(original, operation)(sid)
    after = original.status(sid)
    assert after.tokens_used == 7
    assert after.iterations == 1
    assert after.circuit_breaker_states == before.circuit_breaker_states
    assert after.audit_log == before.audit_log
    assert after.resume_count == (2 if operation == "resume" else 1)
    if operation == "resume":
        assert original.get_supervisor(sid).budget().tokens_used == 7
    if operation != "pause":
        assert old.paused
        assert not old.supports_atomic_checkpoints


@pytest.mark.parametrize("operation", ["stop", "resume"])
def test_lifecycle_merge_never_decreases_checkpoint_counters(operation):
    store = MemoryStore()
    manager = SessionManager(store)
    sid = manager.start("agent", POLICY)
    checkpoint = manager.status(sid)
    checkpoint.tokens_used = 7
    checkpoint.iterations = 2
    store.save_checkpoint(checkpoint)
    getattr(manager, operation)(sid)
    after = manager.status(sid)
    assert after.tokens_used == 7
    assert after.iterations == 2
