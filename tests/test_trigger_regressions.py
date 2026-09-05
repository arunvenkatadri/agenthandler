"""Calendar scheduling and shutdown regression tests."""

import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agenthandler.server import create_app
from agenthandler.session import SessionManager
from agenthandler.store import MemoryStore
from agenthandler.triggers import Scheduler, _cron_matches


@pytest.mark.parametrize("expr", ["0 0 * * 0", "0 0 * * 7"])
def test_cron_zero_and_seven_mean_sunday(expr):
    assert _cron_matches(expr, datetime(2026, 3, 15, tzinfo=timezone.utc))
    assert not _cron_matches(expr, datetime(2026, 3, 16, tzinfo=timezone.utc))


def test_cron_day_fields_use_or_and_steps_start_at_field_minimum():
    assert _cron_matches("0 0 1 * 1", datetime(2026, 3, 16, tzinfo=timezone.utc))
    assert _cron_matches("0 0 1 * 1", datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert _cron_matches("0 0 */2 * *", datetime(2026, 3, 3, tzinfo=timezone.utc))
    assert not _cron_matches("0 0 */2 * *", datetime(2026, 3, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "expr", ["0 0 * * bad", "0 0 * * 8", "0 0 * 13 *", "*/0 * * * *", "0 0 * * 5-2", "0 0 * * 1,9"]
)
def test_invalid_fields_rejected_even_if_earlier_fields_do_not_match(expr):
    with pytest.raises(ValueError):
        _cron_matches(expr, datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_daily_cron_runs_on_consecutive_days_but_only_once_each_minute():
    scheduler = Scheduler()
    run = AsyncMock()
    scheduler.add_cron("daily", run, "0 9 * * *")
    for day in (15, 16):
        with patch("agenthandler.triggers.datetime") as clock:
            clock.now.return_value = datetime(2026, 3, day, 9, tzinfo=timezone.utc)
            await scheduler._tick()
            await scheduler._tick()
    assert run.await_count == 2


def test_stop_cancels_running_trigger_and_closes_loop():
    scheduler = Scheduler()
    started, cancelled = threading.Event(), threading.Event()

    async def job():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scheduler.add_interval("job", job, seconds=0.01)
    scheduler.start()
    loop = scheduler._loop
    try:
        assert started.wait(2)
    finally:
        scheduler.stop()
    assert cancelled.is_set()
    assert loop.is_closed()
    assert scheduler._thread is None
    scheduler.start()
    scheduler.stop()


def test_webhook_disable_enforced_and_unknown_trigger_returns_404():
    app = create_app(SessionManager(MemoryStore()))
    with TestClient(app) as client:
        assert (
            client.post("/triggers", json={"name": "test", "trigger_type": "webhook"}).status_code
            == 201
        )
        assert client.post("/triggers/test/disable").status_code == 200
        assert client.get("/triggers").json()[0]["enabled"] is False
        assert client.post("/triggers/test/fire").status_code == 409
        assert client.post("/triggers/test/enable").status_code == 200
        assert client.post("/triggers/test/fire").status_code == 200
        assert client.post("/triggers/missing/disable").status_code == 404
        assert client.post("/triggers/missing/enable").status_code == 404


def test_server_shutdown_stops_scheduler():
    app = create_app(SessionManager(MemoryStore()))
    with TestClient(app):
        app.state.scheduler.start()
        loop = app.state.scheduler._loop
    assert loop.is_closed()
    assert not app.state.scheduler._running
