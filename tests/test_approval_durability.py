"""Durable approvals — TTL expiry and persistence across restarts."""

import time

from agenthandler.approval import ApprovalQueue, ApprovalStatus


class TestApprovalExpiry:
    def test_submit_with_ttl_sets_expires_at(self):
        q = ApprovalQueue()
        req = q.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=60)
        assert req.expires_at != ""

    def test_pending_request_expires_after_ttl(self):
        q = ApprovalQueue()
        req = q.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=0.01)
        time.sleep(0.02)
        got = q.get(req.approval_id)
        assert got is not None
        assert got.status == ApprovalStatus.EXPIRED

    def test_expired_request_cannot_be_approved(self):
        q = ApprovalQueue()
        req = q.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=0.01)
        time.sleep(0.02)
        assert q.approve(req.approval_id) is None

    def test_expired_requests_excluded_from_list_pending(self):
        q = ApprovalQueue()
        q.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=0.01)
        keeper = q.submit("send_email", {"to": "y"}, "sess1", ttl_seconds=60)
        time.sleep(0.02)
        pending = q.list_pending()
        assert [r.approval_id for r in pending] == [keeper.approval_id]

    def test_no_ttl_means_no_expiry(self):
        q = ApprovalQueue()
        req = q.submit("send_email", {"to": "x"}, "sess1")
        assert req.expires_at == ""
        assert q.get(req.approval_id).status == ApprovalStatus.PENDING

    def test_default_ttl_applies_to_submissions(self):
        q = ApprovalQueue(default_ttl=0.01)
        req = q.submit("send_email", {"to": "x"}, "sess1")
        time.sleep(0.02)
        assert q.get(req.approval_id).status == ApprovalStatus.EXPIRED


class TestApprovalPersistence:
    """A queue given a store survives process restarts (new queue, same store)."""

    def _restart(self, store):
        return ApprovalQueue(store=store)

    def test_pending_approval_survives_restart_memory(self):
        from agenthandler.store import MemoryStore
        store = MemoryStore()
        q1 = ApprovalQueue(store=store)
        req = q1.submit("send_email", {"to": "x"}, "sess1")
        q2 = self._restart(store)
        got = q2.get(req.approval_id)
        assert got is not None
        assert got.status == ApprovalStatus.PENDING
        assert got.tool_name == "send_email"

    def test_resolution_survives_restart_memory(self):
        from agenthandler.store import MemoryStore
        store = MemoryStore()
        q1 = ApprovalQueue(store=store)
        req = q1.submit("send_email", {"to": "x"}, "sess1")
        q1.approve(req.approval_id, approved_by="alice")
        q2 = self._restart(store)
        got = q2.get(req.approval_id)
        assert got.status == ApprovalStatus.APPROVED
        assert got.resolved_by == "alice"

    def test_ttl_still_enforced_after_restart(self):
        from agenthandler.store import MemoryStore
        store = MemoryStore()
        q1 = ApprovalQueue(store=store)
        req = q1.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=0.01)
        time.sleep(0.02)
        q2 = self._restart(store)
        assert q2.get(req.approval_id).status == ApprovalStatus.EXPIRED

    def test_pending_approval_survives_restart_sqlite(self, tmp_path):
        from agenthandler.store import SqliteStore
        db = str(tmp_path / "t.db")
        q1 = ApprovalQueue(store=SqliteStore(db))
        req = q1.submit("send_email", {"to": "x", "n": 3}, "sess1", ttl_seconds=3600)
        q2 = ApprovalQueue(store=SqliteStore(db))
        got = q2.get(req.approval_id)
        assert got is not None
        assert got.status == ApprovalStatus.PENDING
        assert got.tool_args == {"to": "x", "n": 3}
        assert got.expires_at == req.expires_at

    def test_denial_survives_restart_sqlite(self, tmp_path):
        from agenthandler.store import SqliteStore
        db = str(tmp_path / "t.db")
        q1 = ApprovalQueue(store=SqliteStore(db))
        req = q1.submit("send_email", {"to": "x"}, "sess1")
        q1.deny(req.approval_id, reason="nope", denied_by="alice")
        q2 = ApprovalQueue(store=SqliteStore(db))
        got = q2.get(req.approval_id)
        assert got.status == ApprovalStatus.DENIED
        assert got.deny_reason == "nope"

    def test_eviction_removes_from_store(self):
        from agenthandler.store import MemoryStore
        store = MemoryStore()
        q1 = ApprovalQueue(max_requests=2, store=store)
        old = q1.submit("t", {}, "s")
        q1.approve(old.approval_id)
        q1.submit("t", {}, "s")
        q1.submit("t", {}, "s")  # triggers eviction of resolved `old`
        q2 = self._restart(store)
        assert q2.get(old.approval_id) is None


class TestSessionManagerWiring:
    def test_manager_approvals_survive_restart(self, tmp_path):
        from agenthandler.session import SessionManager
        from agenthandler.store import SqliteStore
        db = str(tmp_path / "m.db")
        m1 = SessionManager(store=SqliteStore(db))
        req = m1.approval_queue.submit("send_email", {"to": "x"}, "sess1", ttl_seconds=3600)
        m2 = SessionManager(store=SqliteStore(db))
        got = m2.approval_queue.get(req.approval_id)
        assert got is not None
        assert got.status == ApprovalStatus.PENDING

    def test_manager_approval_ttl_config(self, tmp_path):
        from agenthandler.session import SessionManager
        from agenthandler.store import SqliteStore
        db = str(tmp_path / "m.db")
        m = SessionManager(store=SqliteStore(db), approval_ttl=0.01)
        req = m.approval_queue.submit("send_email", {"to": "x"}, "sess1")
        time.sleep(0.02)
        assert m.approval_queue.get(req.approval_id).status == ApprovalStatus.EXPIRED
