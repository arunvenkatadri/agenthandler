"""Database driver boundaries: read-only mode, transactions, and blocking work."""

import asyncio
import sqlite3
import sys
import threading
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from agenthandler.connectors import MongoConnector, SqlConnector


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO copied FROM source",
        "SELECT secret INTO OUTFILE '/tmp/export' FROM users",
        "WITH x AS (SELECT 1) SELECT * INTO copied FROM x",
        "SELECT 1 /*! INTO OUTFILE '/tmp/export' */",
        "SELECT '/*'; DELETE FROM users; SELECT '*/'",
    ],
)
def test_readonly_rejects_write_forms(sql):
    with pytest.raises(PermissionError):
        SqlConnector("sqlite://:memory:")._check_write(sql)


@pytest.mark.asyncio
async def test_sqlite_returning_is_committed_and_failures_are_rolled_back(tmp_path):
    path = tmp_path / "data.db"
    conn = SqlConnector(f"sqlite:///{path}", allow_write=True)
    try:
        await conn.query("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        inserted = await conn.query("INSERT INTO t VALUES (1) RETURNING id")
        assert inserted["rows"] == [{"id": 1}]
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT id FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.IntegrityError):
            await conn.query("INSERT INTO t VALUES (2), (1)")
        assert (await conn.query("SELECT id FROM t"))["rows"] == [{"id": 1}]
        await conn.query("INSERT INTO t VALUES (3)")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_readonly_sqlite_path_with_uri_characters(tmp_path):
    path = tmp_path / "data?#.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (id INTEGER)")
    conn = SqlConnector(f"sqlite:///{path}")
    try:
        assert (await conn.query("SELECT '; /* literal */' AS value"))["rows"] == [
            {"value": "; /* literal */"}
        ]
    finally:
        conn.close()


def install_driver(monkeypatch, name, child, connection):
    parent = ModuleType(name)
    module = ModuleType(f"{name}.{child}")
    parent.connect = MagicMock(return_value=connection)
    module.connect = MagicMock(return_value=connection)
    setattr(parent, child, module)
    monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(sys.modules, f"{name}.{child}", module)
    return parent, module


@pytest.mark.asyncio
async def test_postgres_readonly_and_parameter_dispatch(monkeypatch):
    connection = MagicMock()
    connection.cursor.return_value.description = [("value",)]
    connection.cursor.return_value.fetchmany.return_value = [("100%",)]
    install_driver(monkeypatch, "psycopg2", "extras", connection)
    conn = SqlConnector("postgresql://user:pass@localhost/test")
    await conn.query("SELECT '100%' AS value")
    connection.set_session.assert_called_once_with(readonly=True)
    connection.cursor.return_value.execute.assert_called_with("SELECT '100%' AS value")
    await conn.query("SELECT %(value)s", params={"value": "bound"})
    connection.cursor.return_value.execute.assert_called_with(
        "SELECT %(value)s", {"value": "bound"}
    )
    assert connection.commit.call_count == 2
    connection.cursor.return_value.execute.side_effect = RuntimeError("bad query")
    with pytest.raises(RuntimeError):
        await conn.query("SELECT 1")
    connection.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_mysql_decodes_credentials_port_and_starts_readonly_transaction(monkeypatch):
    connection = MagicMock()
    connection.cursor.return_value.description = None
    _, driver = install_driver(monkeypatch, "mysql", "connector", connection)
    conn = SqlConnector("mysql://u%40ser:p%40ss@localhost:3307/db%20name")
    await conn.query("SELECT 1")
    driver.connect.assert_called_once_with(
        user="u@ser", password="p@ss", host="localhost", port=3307, database="db name"
    )
    connection.start_transaction.assert_called_once_with(readonly=True)
    connection.commit.assert_called_once()


@pytest.mark.asyncio
async def test_blocking_query_does_not_block_event_loop():
    started, release = threading.Event(), threading.Event()
    connection = MagicMock()
    connection.cursor.return_value.description = None

    def execute(*args):
        started.set()
        release.wait(2)

    connection.cursor.return_value.execute.side_effect = execute
    conn = SqlConnector("sqlite://:memory:")
    conn._conn = connection
    task = asyncio.create_task(conn.query("SELECT 1"))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 1.5)
        assert started.is_set()
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_nonpositive_or_invalid_row_caps_rejected(value):
    with pytest.raises(ValueError):
        SqlConnector("sqlite://:memory:", max_rows=value)
    with pytest.raises(ValueError):
        MongoConnector(max_docs=value)


@pytest.mark.asyncio
async def test_mongo_negative_limit_cannot_bypass_document_cap():
    with pytest.raises(ValueError, match="limit"):
        await MongoConnector(max_docs=5).find(limit=-100)
