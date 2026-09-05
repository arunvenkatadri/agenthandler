"""Triggers and scheduling — run pipelines on schedule, webhook, or data event.

Three trigger types:
- Schedule: cron expression or fixed interval
- Webhook: fires when an HTTP endpoint is called
- Database watch: polls a SQL query and fires when the result changes or
  crosses a threshold

Usage:
    from agenthandler.triggers import Scheduler, WebhookTrigger, DbWatchTrigger

    scheduler = Scheduler()

    # Run every 15 minutes
    scheduler.add_interval("check-alerts", pipeline_fn, minutes=15)

    # Run on cron schedule
    scheduler.add_cron("morning-report", pipeline_fn, cron="0 9 * * *")

    # Run when webhook is called
    webhook = WebhookTrigger("deploy-notify", pipeline_fn)

    # Run when query result crosses threshold
    db_watch = DbWatchTrigger(
        "alert-monitor", pipeline_fn, sql_connector,
        query="SELECT count(*) as cnt FROM alerts WHERE seen=false",
        condition=lambda result: result["rows"][0]["cnt"] > 0,
        poll_seconds=60,
    )

    scheduler.start()  # background thread
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional


@dataclass
class TriggerRecord:
    """Record of a trigger firing."""

    trigger_name: str
    trigger_type: str  # "interval", "cron", "webhook", "db_watch"
    fired_at: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.fired_at:
            self.fired_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "trigger_name": self.trigger_name,
            "trigger_type": self.trigger_type,
            "fired_at": self.fired_at,
        }
        if self.error:
            d["error"] = self.error
        if self.result:
            d["result"] = self.result
        return d


# ---------------------------------------------------------------------------
# Cron parser (minimal — handles standard 5-field cron expressions)
# ---------------------------------------------------------------------------


def _cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a datetime matches a cron expression (minute hour dom month dow)."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron_expr} (need 5 fields)")

    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    # Parse every field before matching so invalid later fields are never hidden.
    allowed = [_cron_values(part, lo, hi) for part, (lo, hi) in zip(parts, ranges)]
    day = dt.day in allowed[2]
    weekday = (dt.weekday() + 1) % 7
    dow = weekday in allowed[4] or (weekday == 0 and 7 in allowed[4])
    day_matches = (
        (day and dow) if parts[2].startswith("*") or parts[4].startswith("*") else (day or dow)
    )
    return (
        dt.minute in allowed[0] and dt.hour in allowed[1] and dt.month in allowed[3] and day_matches
    )


def _cron_values(expression: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for item in expression.split(","):
        base, sep, step_text = item.partition("/")
        step = int(step_text) if sep else 1
        if step <= 0:
            raise ValueError("Cron step must be positive")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = int(base)
            end = hi if sep else start
        if not lo <= start <= end <= hi:
            raise ValueError(f"Cron field outside {lo}..{hi}: {expression}")
        values.update(range(start, end + 1, step))
    return values


def _cron_field_matches(field_expr: str, value: int, lo: int, hi: int) -> bool:
    """Validate a cron field and check membership."""
    return value in _cron_values(field_expr, lo, hi)


# ---------------------------------------------------------------------------
# Trigger entries
# ---------------------------------------------------------------------------


@dataclass
class IntervalEntry:
    name: str
    fn: Callable[[], Coroutine[Any, Any, Any]]
    seconds: float
    last_run: float = 0.0
    enabled: bool = True


@dataclass
class CronEntry:
    name: str
    fn: Callable[[], Coroutine[Any, Any, Any]]
    cron: str
    last_run_minute: int = -1  # track minute to avoid double-firing
    enabled: bool = True


@dataclass
class DbWatchEntry:
    name: str
    fn: Callable[[], Coroutine[Any, Any, Any]]
    connector: Any  # SqlConnector
    query: str
    condition: Callable[[Dict[str, Any]], bool]
    poll_seconds: float = 60.0
    last_poll: float = 0.0
    last_result: Optional[Dict[str, Any]] = None
    enabled: bool = True


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Background scheduler for interval and cron triggers.

    Runs in a daemon thread. Non-blocking.
    """

    def __init__(self) -> None:
        self._intervals: Dict[str, IntervalEntry] = {}
        self._crons: Dict[str, CronEntry] = {}
        self._db_watches: Dict[str, DbWatchEntry] = {}
        self._history: List[TriggerRecord] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def add_interval(
        self,
        name: str,
        fn: Callable[[], Coroutine[Any, Any, Any]],
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
    ) -> None:
        """Add an interval trigger.

        Args:
            name: Unique trigger name.
            fn: Async callable to run (e.g., pipeline.run).
            seconds/minutes/hours: Interval (combined).
        """
        total = seconds + minutes * 60 + hours * 3600
        if not math.isfinite(total) or total <= 0:
            raise ValueError("Interval must be > 0")
        with self._lock:
            self._intervals[name] = IntervalEntry(name=name, fn=fn, seconds=total)

    def add_cron(
        self,
        name: str,
        fn: Callable[[], Coroutine[Any, Any, Any]],
        cron: str,
    ) -> None:
        """Add a cron trigger.

        Args:
            name: Unique trigger name.
            fn: Async callable to run.
            cron: 5-field cron expression (minute hour dom month dow).
        """
        # Validate
        _cron_matches(cron, datetime.now(timezone.utc))
        with self._lock:
            self._crons[name] = CronEntry(name=name, fn=fn, cron=cron)

    def add_db_watch(
        self,
        name: str,
        fn: Callable[[], Coroutine[Any, Any, Any]],
        connector: Any,
        query: str,
        condition: Callable[[Dict[str, Any]], bool],
        poll_seconds: float = 60.0,
    ) -> None:
        """Add a database watch trigger.

        Polls the query at poll_seconds interval. Fires fn when
        condition(query_result) returns True.

        Args:
            name: Unique trigger name.
            fn: Async callable to run when condition fires.
            connector: SqlConnector instance.
            query: SQL query to poll.
            condition: Function that takes query result dict and returns bool.
            poll_seconds: How often to poll.
        """
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        with self._lock:
            self._db_watches[name] = DbWatchEntry(
                name=name,
                fn=fn,
                connector=connector,
                query=query,
                condition=condition,
                poll_seconds=poll_seconds,
            )

    def remove(self, name: str) -> bool:
        """Remove a trigger by name."""
        with self._lock:
            found = False
            if name in self._intervals:
                del self._intervals[name]
                found = True
            if name in self._crons:
                del self._crons[name]
                found = True
            if name in self._db_watches:
                del self._db_watches[name]
                found = True
            return found

    def enable(self, name: str) -> None:
        """Enable a trigger."""
        with self._lock:
            for registry in (self._intervals, self._crons, self._db_watches):
                if name in registry:
                    registry[name].enabled = True

    def disable(self, name: str) -> None:
        """Disable a trigger without removing it."""
        with self._lock:
            for registry in (self._intervals, self._crons, self._db_watches):
                if name in registry:
                    registry[name].enabled = False

    def list_triggers(self) -> List[Dict[str, Any]]:
        """List all registered triggers."""
        with self._lock:
            result: List[Dict[str, Any]] = []
            for ie in self._intervals.values():
                result.append(
                    {
                        "name": ie.name,
                        "type": "interval",
                        "seconds": ie.seconds,
                        "enabled": ie.enabled,
                    }
                )
            for ce in self._crons.values():
                result.append(
                    {
                        "name": ce.name,
                        "type": "cron",
                        "cron": ce.cron,
                        "enabled": ce.enabled,
                    }
                )
            for de in self._db_watches.values():
                result.append(
                    {
                        "name": de.name,
                        "type": "db_watch",
                        "query": de.query,
                        "poll_seconds": de.poll_seconds,
                        "enabled": de.enabled,
                    }
                )
            return result

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent trigger firing history."""
        with self._lock:
            return [r.to_dict() for r in self._history[-limit:]] if limit > 0 else []

    def start(self) -> None:
        """Start one background scheduler thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._running = True
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name="agenthandler-scheduler"
            )
            self._thread.start()

    def stop(self) -> None:
        """Cancel asynchronous work and wait up to five seconds for shutdown."""
        with self._lock:
            self._running = False
            loop, thread = self._loop, self._thread
        if loop is not None:

            def cancel() -> None:
                for task in asyncio.all_tasks(loop):
                    task.cancel()

            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass  # Already closed by the worker.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)

        async def serve() -> None:
            while self._running:
                await self._tick()
                await asyncio.sleep(1)

        try:
            loop.run_until_complete(serve())
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            with self._lock:
                self._running = False
                self._loop = None
                self._thread = None

    async def _tick(self) -> None:
        """Check all triggers and fire any that are due."""
        now = time.monotonic()
        now_dt = datetime.now(timezone.utc)

        with self._lock:
            intervals = list(self._intervals.values())
            crons = list(self._crons.values())
            db_watches = list(self._db_watches.values())

        # Interval triggers
        for ie in intervals:
            if not ie.enabled:
                continue
            if now - ie.last_run >= ie.seconds:
                ie.last_run = now
                await self._fire(ie.name, "interval", ie.fn)

        # Cron triggers
        current_minute = int(now_dt.timestamp() // 60)
        for ce in crons:
            if not ce.enabled:
                continue
            if _cron_matches(ce.cron, now_dt) and ce.last_run_minute != current_minute:
                ce.last_run_minute = current_minute
                await self._fire(ce.name, "cron", ce.fn)

        # DB watch triggers
        for de in db_watches:
            if not de.enabled:
                continue
            if now - de.last_poll >= de.poll_seconds:
                de.last_poll = now
                try:
                    result = await de.connector.query(sql=de.query)
                    if de.condition(result):
                        de.last_result = result
                        await self._fire(de.name, "db_watch", de.fn)
                except Exception as e:
                    self._record(de.name, "db_watch", error=str(e))

    async def _fire(self, name: str, trigger_type: str, fn: Callable[..., Any]) -> None:
        """Fire a trigger and record the result."""
        try:
            result = await fn()
            result_dict = None
            if hasattr(result, "completed"):
                # PipelineResult
                result_dict = {
                    "completed": result.completed,
                    "steps": len(result.steps) if hasattr(result, "steps") else 0,
                }
            self._record(name, trigger_type, result=result_dict)
        except Exception as e:
            self._record(name, trigger_type, error=str(e))

    def _record(
        self,
        name: str,
        trigger_type: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record a trigger firing in history."""
        record = TriggerRecord(
            trigger_name=name,
            trigger_type=trigger_type,
            result=result,
            error=error,
        )
        with self._lock:
            self._history.append(record)
            if len(self._history) > 1000:
                self._history = self._history[-500:]


# ---------------------------------------------------------------------------
# WebhookTrigger — fires when an HTTP endpoint is called
# ---------------------------------------------------------------------------


class WebhookTrigger:
    """A trigger that fires when its HTTP endpoint is called.

    Register with the REST server via Scheduler or directly.

    Usage:
        trigger = WebhookTrigger("deploy", pipeline.run)
        # POST /triggers/deploy → fires pipeline.run()
    """

    def __init__(
        self,
        name: str,
        fn: Callable[[], Coroutine[Any, Any, Any]],
    ):
        self.name = name
        self.fn = fn
        self.enabled = True
        self._history: List[TriggerRecord] = []

    async def fire(self, payload: Optional[Dict[str, Any]] = None) -> TriggerRecord:
        """Fire the trigger. Called by the REST endpoint handler."""
        if not self.enabled:
            raise PermissionError("Webhook is disabled")
        try:
            result = await self.fn()
            result_dict = None
            if hasattr(result, "completed"):
                result_dict = {
                    "completed": result.completed,
                    "steps": len(result.steps) if hasattr(result, "steps") else 0,
                }
            record = TriggerRecord(
                trigger_name=self.name,
                trigger_type="webhook",
                result=result_dict,
            )
        except Exception as e:
            record = TriggerRecord(
                trigger_name=self.name,
                trigger_type="webhook",
                error=str(e),
            )
        self._history.append(record)
        self._history = self._history[-1000:]
        return record

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._history[-limit:]] if limit > 0 else []
