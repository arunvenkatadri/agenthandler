"""Optional, sequential task execution with durable intent and acceptance checks.

Callbacks are trusted application code. They receive detached state, not a
mutable checkpoint. Reconciliation and verification must be safe to repeat.
The task store is separate from agent-writable SessionManager payloads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Sequence

from .completion import CompletionStatus, VerificationResult
from .session import SessionManager
from .store import SessionStatus
from .supervisor import Supervisor


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _nonnegative(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Budget values must be non-negative integers")


@dataclass(frozen=True)
class CallBudget:
    """Conservative upper bounds charged BEFORE invoking a callback.

    Adapters must enforce these bounds at the provider. Reservations are never
    refunded, including on crashes. Zero means the callback consumes no model
    tokens or paid services, not unlimited usage. One dollar = 1,000,000 microUSD.
    """

    tokens: int = 0
    cost_microusd: int = 0

    def __post_init__(self) -> None:
        _nonnegative(self.tokens)
        _nonnegative(self.cost_microusd)


@dataclass(frozen=True)
class TaskLimits:
    max_calls: int = 100
    max_tokens: Optional[int] = None
    max_cost_microusd: Optional[int] = None

    def __post_init__(self) -> None:
        _nonnegative(self.max_calls)
        for value in (self.max_tokens, self.max_cost_microusd):
            if value is not None:
                _nonnegative(value)


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    goal: str
    milestone_id: str
    acceptance: str
    operation_id: str
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    output: Any
    budget: CallBudget


@dataclass(frozen=True)
class RecoveryResult:
    """Authoritative lookup of a previous operation.

    ``completed`` supplies its output; ``absent`` certifies it did not execute
    and cannot execute later; ``unknown`` blocks replay. Eventual-consistency
    lookup misses and timed-out operations still running are ``unknown``.
    """

    status: str
    output: Any = None
    reason: str = ""


@dataclass(frozen=True)
class Milestone:
    id: str
    acceptance: str
    execute: Callable[[TaskContext], Awaitable[Any]]
    verify: Callable[[TaskContext], Awaitable[VerificationResult]]
    reconcile: Optional[Callable[[TaskContext], Awaitable[RecoveryResult]]] = None
    version: str = "1"
    action_budget: CallBudget = field(default_factory=CallBudget)
    verification_budget: CallBudget = field(default_factory=CallBudget)
    recovery_budget: CallBudget = field(default_factory=CallBudget)

    def spec(self) -> Dict[str, Any]:
        if not self.id or not self.acceptance or not self.version:
            raise ValueError("Milestones require an id, acceptance criteria, and version")
        return {
            "id": self.id,
            "acceptance": self.acceptance,
            "version": self.version,
            "reconcile": self.reconcile is not None,
            "action_budget": asdict(self.action_budget),
            "verification_budget": asdict(self.verification_budget),
            "recovery_budget": asdict(self.recovery_budget),
        }


@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    goal: str
    specs: List[Dict[str, Any]]
    limits: Dict[str, Any]
    template_id: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    milestones: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    status: CompletionStatus = CompletionStatus.RUNNING
    reason: str = ""
    calls_reserved: int = 0
    tokens_reserved: int = 0
    cost_reserved_microusd: int = 0

    @property
    def completed(self) -> bool:
        return self.status == CompletionStatus.VERIFIED

    @property
    def next_milestone(self) -> Optional[str]:
        return next(
            (s["id"] for s in self.specs if self.milestones[s["id"]]["state"] != "verified"),
            None,
        )


class TaskBusyError(RuntimeError):
    """Another worker owns this task store."""


class SqliteTaskStore:
    """Durable task records with an OS-released worker lock.

    One active runner per store, including across processes. A separate SQLite
    lock database holds the worker lock while the main database commits progress.
    Process death releases the lock without rolling back committed reservations.
    Use a local filesystem; network SQLite filesystems are not supported.
    """

    def __init__(self, path: str):
        if path == ":memory:":
            raise ValueError("A durable task store requires a filesystem path")
        self.path = str(Path(path).resolve())
        for filename in (self.path, self.path + ".worker-lock"):
            fd = os.open(filename, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def claim(self) -> Iterator[None]:
        db = sqlite3.connect(self.path + ".worker-lock", timeout=0)
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise TaskBusyError("A worker already owns this task store") from exc
            yield
        finally:
            db.rollback()
            db.close()

    def load(self, task_id: str) -> TaskRecord:
        with self._connection() as db:
            row = db.execute("SELECT record FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        value = json.loads(row[0])
        value["status"] = CompletionStatus(value["status"])
        return TaskRecord(**value)

    def list_tasks(self, limit: int = 100) -> List[TaskRecord]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT record FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        records = []
        for row in rows:
            value = json.loads(row[0])
            value["status"] = CompletionStatus(value["status"])
            records.append(TaskRecord(**value))
        return records

    def _save(self, record: TaskRecord, *, create: bool = False) -> None:
        value = json.dumps(asdict(record), allow_nan=False)
        with self._connection() as db:
            if create:
                db.execute("INSERT INTO tasks VALUES (?, ?)", (record.task_id, value))
            else:
                db.execute("UPDATE tasks SET record = ? WHERE id = ?", (value, record.task_id))


class _Blocked(Exception):
    pass


class DurableTaskRunner:
    """Opt-in orchestration above SessionManager/Supervisor, without a model SDK.

    The supplied milestones define the entire acceptance contract. Change their
    version when callback behavior changes; resume rejects changed specifications.
    Callbacks must use operation_id for provider idempotency where available.
    """

    def __init__(self, manager: SessionManager, store: SqliteTaskStore):
        self.manager = manager
        self.store = store

    def create(
        self,
        agent_id: str,
        goal: str,
        milestones: Sequence[Milestone],
        *,
        limits: Optional[TaskLimits] = None,
        policy_dict: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        template_id: Optional[str] = None,
    ) -> TaskRecord:
        specs = [m.spec() for m in milestones]
        if not goal or not specs or len({s["id"] for s in specs}) != len(specs):
            raise ValueError("A task needs a goal and at least one uniquely named milestone")
        safe_inputs = _json_copy(inputs or {})
        limits = limits or TaskLimits()
        with self.store.claim():
            sid = self.manager.start(agent_id, policy_dict or {"max_iterations": limits.max_calls})
            record = TaskRecord(
                task_id=uuid.uuid4().hex,
                session_id=sid,
                goal=goal,
                specs=specs,
                limits=asdict(limits),
                template_id=template_id,
                inputs=safe_inputs,
                milestones={
                    s["id"]: {"state": "ready", "operation_id": uuid.uuid4().hex} for s in specs
                },
            )
            self.store._save(record, create=True)
        return record

    async def run(self, task_id: str, milestones: Sequence[Milestone]) -> TaskRecord:
        with self.store.claim():
            record = self.store.load(task_id)
            if record.specs != [m.spec() for m in milestones]:
                raise ValueError(
                    "Task specification changed; resume requires the original contract"
                )
            if record.completed:
                return record
            try:
                checkpoint = self.manager.status(record.session_id)
                if checkpoint is None or checkpoint.status != SessionStatus.RUNNING:
                    raise _Blocked(
                        "Session is not running; explicit session authorization required"
                    )
                sv = self.manager.get_supervisor(record.session_id)
                if sv is None:
                    sv = self.manager.resume(record.session_id)
                record.status = CompletionStatus.RUNNING
                record.reason = ""
                for milestone in milestones:
                    state = record.milestones[milestone.id]
                    if state["state"] == "verified":
                        continue
                    if state["state"] == "pending":
                        if milestone.reconcile is None:
                            raise _Blocked("Uncertain operation requires reconciliation")
                        recovered = await self._call(
                            record,
                            milestone,
                            sv,
                            "recover",
                            milestone.reconcile,
                            milestone.recovery_budget,
                        )
                        if not isinstance(recovered, RecoveryResult):
                            raise ValueError("Reconciler must return RecoveryResult")
                        if recovered.status == "completed":
                            state["output"] = _json_copy(recovered.output)
                            state["state"] = "executed"
                        elif recovered.status == "absent":
                            state["state"] = "ready"
                        elif recovered.status == "unknown":
                            raise _Blocked(recovered.reason or "Operation outcome is still unknown")
                        else:
                            raise ValueError("Invalid recovery status")
                        self.store._save(record)
                    if state["state"] == "ready":
                        output = await self._call(
                            record,
                            milestone,
                            sv,
                            "execute",
                            milestone.execute,
                            milestone.action_budget,
                        )
                        state["output"] = _json_copy(output)
                        state["state"] = "executed"
                        self.store._save(record)
                    record.status = CompletionStatus.PROPOSED
                    self.store._save(record)
                    verification = await self._call(
                        record,
                        milestone,
                        sv,
                        "verify",
                        milestone.verify,
                        milestone.verification_budget,
                    )
                    if not isinstance(verification, VerificationResult):
                        raise ValueError("Verifier must return VerificationResult")
                    verification.validate()
                    state["verification"] = _json_copy(asdict(verification))
                    if not verification.passed:
                        raise _Blocked(verification.reason or "Acceptance verification failed")
                    state["state"] = "verified"
                    record.status = CompletionStatus.RUNNING
                    self.store._save(record)
                record.status = CompletionStatus.VERIFIED
                record.reason = "All milestone acceptance checks passed"
            except _Blocked as exc:
                record.status = CompletionStatus.BLOCKED
                record.reason = str(exc)
            except (ValueError, TypeError) as exc:
                record.status = CompletionStatus.INVALID
                record.reason = str(exc)
            except Exception as exc:
                record.status = CompletionStatus.BLOCKED
                record.reason = str(exc)
            self.store._save(record)
            return record

    async def _call(
        self,
        record: TaskRecord,
        milestone: Milestone,
        sv: Supervisor,
        phase: str,
        callback: Callable[[TaskContext], Awaitable[Any]],
        budget: CallBudget,
    ) -> Any:
        reservations = (
            (record.calls_reserved + 1, record.limits["max_calls"]),
            (record.tokens_reserved + budget.tokens, record.limits["max_tokens"]),
            (
                record.cost_reserved_microusd + budget.cost_microusd,
                record.limits["max_cost_microusd"],
            ),
        )
        if any(limit is not None and used > limit for used, limit in reservations):
            raise _Blocked("Task budget exhausted")
        state = record.milestones[milestone.id]
        record.calls_reserved += 1
        record.tokens_reserved += budget.tokens
        record.cost_reserved_microusd += budget.cost_microusd
        if phase == "execute":
            state["state"] = "pending"
        # This commit precedes every callback, even across process death.
        state["last_phase"] = phase
        self.store._save(record)
        context = TaskContext(
            task_id=record.task_id,
            goal=record.goal,
            milestone_id=milestone.id,
            acceptance=milestone.acceptance,
            operation_id=state["operation_id"],
            inputs=_json_copy(record.inputs),
            outputs={
                k: _json_copy(v["output"])
                for k, v in record.milestones.items()
                if v["state"] == "verified"
            },
            output=_json_copy(state.get("output")),
            budget=budget,
        )
        active, invoked, returned = True, False, False

        async def invoke(operation_id: str) -> Any:
            nonlocal invoked, returned
            # Supervisor retries, fallbacks and deferred approvals must not replay
            # the callback outside this durable reservation and worker lock.
            if not active or invoked:
                raise RuntimeError("Durable callback cannot be replayed by Supervisor")
            invoked = True
            output = await callback(context)
            returned = True
            return output

        try:
            sv.record_iteration()
            result = await sv.call(
                f"task.{milestone.id}.{phase}", invoke, operation_id=context.operation_id
            )
        finally:
            active = False
        if not result.succeeded or not returned:
            reason = result.error.user_message() if result.error else "Callback did not execute"
            raise _Blocked(reason)
        return result.output
