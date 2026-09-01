"""Approval queue — human-in-the-loop confirmation for dangerous tool calls.

When a tool is in the policy's require_confirm list, the Supervisor queues
the call here instead of blocking it outright. The call stays pending until
a human approves or denies it via the REST API, dashboard, or any client
listening on the WebSocket events stream.

Durability: pass a store with approval support (MemoryStore/SqliteStore) and
every mutation is written through, so pending approvals survive process
restarts. Pass ``default_ttl`` (or per-call ``ttl_seconds``) to expire
approvals that nobody resolves; expiry is applied lazily on access.

Usage (internal — SessionManager wires this up automatically):

    queue = ApprovalQueue()
    approval_id = queue.submit("delete_file", {"path": "/tmp/data"}, session_id)
    # ... user reviews and approves via REST ...
    queue.approve(approval_id)
    # ... or denies ...
    queue.deny(approval_id, reason="Too risky")
"""

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    """A pending tool call awaiting human approval."""

    approval_id: str
    session_id: str
    tool_name: str
    tool_args: Dict[str, Any]
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = ""
    resolved_at: str = ""
    resolved_by: str = ""  # who approved/denied (for audit)
    deny_reason: str = ""
    expires_at: str = ""  # ISO timestamp; empty = never expires

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_args": {k: str(v)[:200] for k, v in self.tool_args.items()},
            "status": self.status.value,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "deny_reason": self.deny_reason,
            "expires_at": self.expires_at,
        }

    def to_storage_dict(self) -> Dict[str, Any]:
        """Full-fidelity dict for persistence (to_dict truncates tool_args)."""
        d = self.to_dict()
        d["tool_args"] = self.tool_args
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ApprovalRequest":
        return cls(
            approval_id=d["approval_id"],
            session_id=d["session_id"],
            tool_name=d["tool_name"],
            tool_args=d.get("tool_args", {}),
            status=ApprovalStatus(d.get("status", "pending")),
            created_at=d.get("created_at", ""),
            resolved_at=d.get("resolved_at", ""),
            resolved_by=d.get("resolved_by", ""),
            deny_reason=d.get("deny_reason", ""),
            expires_at=d.get("expires_at", ""),
        )


class ApprovalQueue:
    """Thread-safe queue of pending tool call approvals.

    One queue per SessionManager — holds approvals for all sessions.

    Args:
        max_requests: Maximum number of requests to keep. When exceeded,
                      oldest resolved (non-pending) requests are evicted.
                      New submissions are rejected if the queue is full
                      even after eviction.
    """

    def __init__(
        self,
        max_requests: int = 10000,
        default_ttl: Optional[float] = None,
        store: Optional[Any] = None,
    ) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}
        self._max_requests = max_requests
        self._default_ttl = default_ttl
        self._store = store if store is not None and hasattr(store, "save_approval") else None
        self._lock = threading.Lock()
        if self._store is not None:
            for req in self._store.load_approvals():
                self._requests[req.approval_id] = req

    def _persist(self, req: ApprovalRequest) -> None:
        """Write-through to the store, if configured. Must hold _lock."""
        if self._store is not None:
            self._store.save_approval(req)

    def _expire_due(self) -> None:
        """Mark overdue pending requests EXPIRED. Must hold _lock."""
        now = datetime.now(timezone.utc).isoformat()
        for req in self._requests.values():
            if (
                req.status == ApprovalStatus.PENDING
                and req.expires_at
                and req.expires_at <= now
            ):
                req.status = ApprovalStatus.EXPIRED
                req.resolved_at = now
                self._persist(req)

    def _evict_resolved(self) -> None:
        """Remove oldest resolved requests to make room. Must hold _lock."""
        resolved = [
            (aid, req)
            for aid, req in self._requests.items()
            if req.status != ApprovalStatus.PENDING
        ]
        # Sort by resolved_at (or created_at as fallback) so oldest go first
        resolved.sort(key=lambda pair: pair[1].resolved_at or pair[1].created_at)
        to_remove = len(self._requests) - self._max_requests + 1
        for aid, _ in resolved[:to_remove]:
            del self._requests[aid]
            if self._store is not None:
                self._store.delete_approval(aid)

    def submit(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        session_id: str,
        ttl_seconds: Optional[float] = None,
    ) -> ApprovalRequest:
        """Queue a tool call for approval. Returns the ApprovalRequest.

        Raises RuntimeError if the queue is full and cannot evict enough
        resolved requests to make room.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires_at = ""
        if ttl is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl)
            ).isoformat()
        req = ApprovalRequest(
            approval_id=secrets.token_hex(8),
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            expires_at=expires_at,
        )
        with self._lock:
            if len(self._requests) >= self._max_requests:
                self._evict_resolved()
                if len(self._requests) >= self._max_requests:
                    raise RuntimeError(
                        f"Approval queue full ({self._max_requests} requests). "
                        "Resolve existing approvals before submitting new ones."
                    )
            self._requests[req.approval_id] = req
            self._persist(req)
        return req

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            self._expire_due()
            return self._requests.get(approval_id)

    def approve(self, approval_id: str, approved_by: str = "") -> Optional[ApprovalRequest]:
        """Approve a pending request. Returns the request, or None if not found."""
        with self._lock:
            self._expire_due()
            req = self._requests.get(approval_id)
            if req is None or req.status != ApprovalStatus.PENDING:
                return None
            req.status = ApprovalStatus.APPROVED
            req.resolved_at = datetime.now(timezone.utc).isoformat()
            req.resolved_by = approved_by
            self._persist(req)
            return req

    def deny(
        self, approval_id: str, reason: str = "", denied_by: str = ""
    ) -> Optional[ApprovalRequest]:
        """Deny a pending request. Returns the request, or None if not found."""
        with self._lock:
            self._expire_due()
            req = self._requests.get(approval_id)
            if req is None or req.status != ApprovalStatus.PENDING:
                return None
            req.status = ApprovalStatus.DENIED
            req.deny_reason = reason
            req.resolved_at = datetime.now(timezone.utc).isoformat()
            req.resolved_by = denied_by
            self._persist(req)
            return req

    def list_pending(self, session_id: Optional[str] = None) -> List[ApprovalRequest]:
        """List pending approvals, optionally filtered by session."""
        with self._lock:
            self._expire_due()
            reqs = list(self._requests.values())
        if session_id is not None:
            reqs = [r for r in reqs if r.session_id == session_id]
        return [r for r in reqs if r.status == ApprovalStatus.PENDING]

    def list_all(self, session_id: Optional[str] = None) -> List[ApprovalRequest]:
        """List all approvals (any status), optionally filtered by session."""
        with self._lock:
            self._expire_due()
            reqs = list(self._requests.values())
        if session_id is not None:
            reqs = [r for r in reqs if r.session_id == session_id]
        return reqs
