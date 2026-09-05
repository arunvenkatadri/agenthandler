"""Database connectors — supervised query tools for SQL and NoSQL.

Give agents the ability to query databases as regular supervised tool calls.
Results flow through the full AgentHandler pipeline: PII redaction, audit logging,
tool scoping, budgets, and observability.

Connectors return async callables that work with ``supervisor.call()`` and
``Pipeline.add_step()``.

SQL (PostgreSQL, MySQL, SQLite) — uses DB-API 2.0 (stdlib sqlite3 or
any PEP 249 driver). No required dependencies beyond stdlib.

MongoDB — optional, requires ``pymongo``.

Usage:
    from agenthandler.connectors import SqlConnector

    sql = SqlConnector("sqlite:///data.db")
    result = await supervisor.call("query", sql.query, sql="SELECT * FROM users LIMIT 10")

    # In a pipeline
    pipeline.add_step("get_metrics", sql.query, sql="SELECT avg(price) FROM orders")
    pipeline.add_step("analyze", my_analyze_fn)
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

# Regex patterns for stripping SQL comments before safety checks
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")

# DML / DDL keywords that must never appear in read-only queries
_WRITE_KEYWORDS = frozenset(
    {
        "INTO",
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "REPLACE",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "COPY",
        "LOAD",
        "GRANT",
        "REVOKE",
    }
)

# Valid table-name pattern (alphanumeric + underscore only)
_SAFE_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _redact_connection_string(conn_str: str) -> str:
    """Return a redacted form of *conn_str* safe for error messages.

    Credentials (userinfo component) are replaced with ``***``.  SQLite
    paths and unrecognised formats are returned as-is since they carry no
    credentials.
    """
    if conn_str.startswith("sqlite"):
        return conn_str  # no credentials to redact
    try:
        parsed = urlparse(conn_str)
        if parsed.username or parsed.password:
            # Rebuild with redacted userinfo
            host_part = parsed.hostname or ""
            if parsed.port:
                host_part += f":{parsed.port}"
            return f"{parsed.scheme}://***:***@{host_part}{parsed.path}"
    except Exception:
        pass
    return "<redacted>"


def _strip_sql_comments(sql: str) -> str:
    """Remove block ``/* */`` and line ``--`` comments from *sql*."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return sql


def _validate_table_name(name: str) -> None:
    """Raise ValueError if *name* is not a safe SQL identifier."""
    if not _SAFE_TABLE_NAME_RE.match(name):
        raise ValueError(
            f"Unsafe table name rejected: {name!r}. "
            "Only alphanumeric characters and underscores are allowed."
        )


class SqlConnector:
    """Supervised SQL query tool.

    Connects to SQLite (stdlib), PostgreSQL, or MySQL via DB-API 2.0.
    Returns an async callable for use with Supervisor.call().

    Args:
        connection_string: Database connection string.
            - SQLite: "sqlite:///path/to/db.sqlite" or "sqlite://:memory:"
            - PostgreSQL: "postgresql://user:pass@host/db" (requires psycopg2)
            - MySQL: "mysql://user:pass@host/db" (requires mysql-connector-python)
        allow_write: If False (default), only SELECT statements are allowed.
                    INSERT, UPDATE, DELETE, DROP, ALTER, etc. are blocked.
        max_rows: Maximum rows to return per query (default: 1000).
    """

    def __init__(
        self,
        connection_string: str,
        allow_write: bool = False,
        max_rows: int = 1000,
    ):
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
            raise ValueError("max_rows must be a positive integer")
        self._lock = threading.RLock()
        self._conn_str = connection_string
        self._allow_write = allow_write
        self._max_rows = max_rows
        self._conn: Any = None

    def _connect(self) -> Any:
        """Lazily connect to the database."""
        if self._conn is not None:
            return self._conn

        if self._conn_str.startswith("sqlite:///"):
            path = self._conn_str[len("sqlite:///") :]
            if not self._allow_write:
                # Use URI mode with ?mode=ro for defense-in-depth read-only
                uri = Path(path).resolve().as_uri() + "?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        elif self._conn_str == "sqlite://:memory:":
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        elif self._conn_str.startswith("postgresql://"):
            try:
                import psycopg2
                import psycopg2.extras

                conn = psycopg2.connect(self._conn_str)
                try:
                    if not self._allow_write:
                        conn.set_session(readonly=True)
                except Exception:
                    conn.close()
                    raise
                self._conn = conn
            except ImportError as e:
                raise ImportError(
                    "psycopg2 required for PostgreSQL: pip install psycopg2-binary"
                ) from e
        elif self._conn_str.startswith("mysql://"):
            try:
                import mysql.connector  # type: ignore[import-not-found]

                parsed = urlparse(self._conn_str)
                if not parsed.hostname or not parsed.path.strip("/"):
                    raise ValueError("Invalid MySQL connection string")
                self._conn = mysql.connector.connect(
                    user=unquote(parsed.username or ""),
                    password=unquote(parsed.password or ""),
                    host=parsed.hostname,
                    port=parsed.port or 3306,
                    database=unquote(parsed.path.lstrip("/")),
                )
            except ImportError as e:
                raise ImportError(
                    "mysql-connector-python required for MySQL: pip install mysql-connector-python"
                ) from e
        else:
            raise ValueError(
                f"Unsupported connection string: "
                f"{_redact_connection_string(self._conn_str)}. "
                "Use sqlite:///path, postgresql://..., or mysql://..."
            )
        return self._conn

    def _check_write(self, sql: str) -> None:
        """Block write operations if allow_write is False.

        Uses an *allowlist* approach: only ``SELECT`` and read-only ``WITH``
        (CTE) statements are permitted.  SQL comments are stripped first so
        that ``--`` and ``/* */`` tricks cannot hide DML.  Multi-statement
        strings (containing ``;``) are rejected outright.
        """
        if self._allow_write:
            return

        # Strip comments so they cannot be used to hide write operations
        if "/*!" in sql:
            raise PermissionError("Executable SQL comments are not allowed in read-only mode.")
        cleaned = re.sub(
            r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|--[^\n]*|/\*.*?\*/",
            " ",
            sql,
            flags=re.DOTALL,
        )
        normalized = cleaned.strip().upper()

        if not normalized:
            raise PermissionError("Empty SQL statement after comment stripping.")

        # Reject multi-statement queries (e.g. "SELECT 1; DROP TABLE x")
        # Split on semicolons, ignore trailing empty parts
        statements = [s.strip() for s in normalized.split(";") if s.strip()]
        if len(statements) > 1:
            raise PermissionError("Multi-statement queries are not allowed in read-only mode.")

        # Allowlist: statement must start with SELECT or WITH
        first_word = normalized.split()[0] if normalized.split() else ""
        if first_word in {"SELECT", "WITH"}:
            tokens = set(re.findall(r"[A-Z_]+", normalized))
            found = tokens & _WRITE_KEYWORDS
            if found:
                raise PermissionError(
                    f"Write keyword(s) {', '.join(sorted(found))} are not allowed "
                    "in read-only mode. Set allow_write=True to enable."
                )
            return

        # Anything else (INSERT, UPDATE, DELETE, DROP, PRAGMA, …) is blocked
        raise PermissionError(
            f"Statement type '{first_word}' is not allowed in read-only mode. "
            f"Only SELECT and read-only WITH/CTE are permitted. "
            f"Set allow_write=True to enable write operations."
        )

    async def query(
        self,
        sql: str = "",
        params: Optional[Dict[str, Any]] = None,
        input: str = "",
    ) -> Dict[str, Any]:
        """Execute a SQL query and return results.

        Args:
            sql: The SQL query to execute. If empty, uses ``input`` (for pipeline chaining).
            params: Query parameters (for parameterized queries).
            input: Alternative to ``sql`` — used when chained from a previous pipeline step.

        Returns:
            Dict with "rows" (list of dicts), "row_count", and "columns".
        """
        return await asyncio.to_thread(self._query, sql or input, params)

    def _query(self, query_str: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            return self._query_locked(query_str, params)

    def _query_locked(self, query_str: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not query_str:
            return {"rows": [], "row_count": 0, "columns": []}

        self._check_write(query_str)
        conn = self._connect()
        try:
            if self._conn_str.startswith("mysql://"):
                if not self._allow_write:
                    conn.start_transaction(readonly=True)
                cursor = conn.cursor(buffered=True)
            else:
                cursor = conn.cursor()
            try:
                if params is None:
                    cursor.execute(query_str)
                else:
                    cursor.execute(query_str, params)
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchmany(self._max_rows)]
                    result = {"rows": rows, "row_count": len(rows), "columns": columns}
                else:
                    result = {
                        "rows": [],
                        "row_count": cursor.rowcount,
                        "columns": [],
                        "affected_rows": cursor.rowcount,
                    }
            finally:
                cursor.close()
            # RETURNING also has a result set, but still needs a commit.
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    async def execute(
        self,
        sql: str = "",
        params: Optional[Dict[str, Any]] = None,
        input: str = "",
    ) -> Dict[str, Any]:
        """Execute a write SQL statement. Alias for query() with write intent.

        Raises PermissionError if allow_write is False.
        """
        return await self.query(sql=sql, params=params, input=input)

    async def discover_schema(self) -> Dict[str, Any]:
        """Discover the database schema — tables, columns, types.

        Returns a dict describing every table and its columns, suitable
        for passing to an LLM so it can write correct SQL.

        Returns:
            {"tables": [{"name": "...", "columns": [{"name": "...", "type": "..."}]}]}
        """
        return await asyncio.to_thread(self._discover_schema)

    def _discover_schema(self) -> Dict[str, Any]:
        with self._lock:
            try:
                return self._discover_schema_locked()
            finally:
                if self._conn is not None:
                    self._conn.rollback()

    def _discover_schema_locked(self) -> Dict[str, Any]:
        conn = self._connect()
        tables: List[Dict[str, Any]] = []

        cursor = conn.cursor()
        try:
            if self._conn_str.startswith("sqlite"):
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                table_names = [row[0] for row in cursor.fetchall()]
                for tname in table_names:
                    if tname.startswith("sqlite_"):
                        continue
                    # Validate table name to prevent SQL injection via
                    # crafted table names stored in sqlite_master.
                    _validate_table_name(tname)
                    cursor.execute(f"PRAGMA table_info([{tname}])")
                    cols = [
                        {
                            "name": row[1],
                            "type": row[2],
                            "nullable": not row[3],
                            "pk": bool(row[5]),
                        }
                        for row in cursor.fetchall()
                    ]
                    # Sample a few rows for context
                    cursor.execute(f"SELECT COUNT(*) FROM [{tname}]")
                    row_count = cursor.fetchone()[0]
                    tables.append(
                        {
                            "name": tname,
                            "columns": cols,
                            "row_count": row_count,
                        }
                    )

            elif self._conn_str.startswith("postgresql"):
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
                table_names = [row[0] for row in cursor.fetchall()]
                for tname in table_names:
                    cursor.execute(
                        "SELECT column_name, data_type, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_name = %s ORDER BY ordinal_position",
                        (tname,),
                    )
                    cols = [
                        {
                            "name": row[0],
                            "type": row[1],
                            "nullable": row[2] == "YES",
                            "default": row[3],
                        }
                        for row in cursor.fetchall()
                    ]
                    tables.append({"name": tname, "columns": cols})

            elif self._conn_str.startswith("mysql"):
                cursor.execute("SHOW TABLES")
                table_names = [row[0] for row in cursor.fetchall()]
                for tname in table_names:
                    _validate_table_name(tname)
                    cursor.execute(f"DESCRIBE `{tname}`")
                    cols = [
                        {
                            "name": row[0],
                            "type": row[1],
                            "nullable": row[2] == "YES",
                            "pk": row[3] == "PRI",
                        }
                        for row in cursor.fetchall()
                    ]
                    tables.append({"name": tname, "columns": cols})

        finally:
            cursor.close()

        return {"tables": tables}

    def schema_to_text(self, schema: Dict[str, Any]) -> str:
        """Format a schema dict as human-readable text for LLM prompts.

        Example output:
            Table: orders (1500 rows)
              - id INTEGER (PK)
              - product TEXT
              - amount REAL
        """
        lines = []
        for table in schema.get("tables", []):
            header = f"Table: {table['name']}"
            if "row_count" in table:
                header += f" ({table['row_count']} rows)"
            lines.append(header)
            for col in table.get("columns", []):
                parts = [f"  - {col['name']} {col.get('type', '')}"]
                if col.get("pk"):
                    parts.append("(PK)")
                if col.get("nullable") is False:
                    parts.append("NOT NULL")
                lines.append(" ".join(parts))
            lines.append("")
        return "\n".join(lines)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class MongoConnector:
    """Supervised MongoDB query tool.

    Requires ``pymongo``: pip install pymongo

    Args:
        uri: MongoDB connection URI (e.g., "mongodb://localhost:27017").
        database: Database name.
        allow_write: If False (default), only find operations are allowed.
        max_docs: Maximum documents to return per query (default: 1000).
    """

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "default",
        allow_write: bool = False,
        max_docs: int = 1000,
    ):
        if isinstance(max_docs, bool) or not isinstance(max_docs, int) or max_docs <= 0:
            raise ValueError("max_docs must be a positive integer")
        self._uri = uri
        self._database = database
        self._allow_write = allow_write
        self._max_docs = max_docs
        self._client: Any = None
        self._db: Any = None

    def _connect(self) -> Any:
        """Lazily connect to MongoDB."""
        if self._db is not None:
            return self._db
        try:
            import pymongo

            self._client = pymongo.MongoClient(self._uri)
            self._db = self._client[self._database]
        except ImportError as e:
            raise ImportError("pymongo required: pip install pymongo") from e
        return self._db

    async def find(
        self,
        collection: str = "",
        filter: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        sort: Optional[List[Any]] = None,
        limit: Optional[int] = None,
        input: str = "",
    ) -> Dict[str, Any]:
        """Query a MongoDB collection.

        Args:
            collection: Collection name.
            filter: MongoDB query filter.
            projection: Fields to include/exclude.
            sort: Sort specification.
            limit: Max documents (capped at max_docs).
            input: Unused — for pipeline chaining compatibility.

        Returns:
            Dict with "documents" (list of dicts) and "count".
        """
        return await asyncio.to_thread(self._find, collection, filter, projection, sort, limit)

    def _find(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]],
        projection: Optional[Dict[str, Any]],
        sort: Optional[List[Any]],
        limit: Optional[int],
    ) -> Dict[str, Any]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer")
        db = self._connect()
        coll = db[collection]
        cursor = coll.find(
            filter=filter or {},
            projection=projection,
        )
        if sort:
            cursor = cursor.sort(sort)
        cursor = cursor.limit(min(limit or self._max_docs, self._max_docs))

        docs = []
        for doc in cursor:
            # Convert ObjectId to string for JSON serialization
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            docs.append(doc)

        return {"documents": docs, "count": len(docs)}

    async def insert(
        self,
        collection: str = "",
        documents: Optional[List[Dict[str, Any]]] = None,
        input: str = "",
    ) -> Dict[str, Any]:
        """Insert documents into a MongoDB collection.

        Raises PermissionError if allow_write is False.
        """
        return await asyncio.to_thread(self._insert, collection, documents)

    def _insert(self, collection: str, documents: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        if not self._allow_write:
            raise PermissionError("Write operation blocked. Set allow_write=True to enable.")
        db = self._connect()
        coll = db[collection]
        docs = documents or []
        if not docs:
            return {"inserted_count": 0}
        result = coll.insert_many(docs)
        return {"inserted_count": len(result.inserted_ids)}

    async def discover_schema(self) -> Dict[str, Any]:
        """Discover MongoDB collections and sample field structures.

        Samples a few documents from each collection to infer the schema.
        """
        return await asyncio.to_thread(self._discover_schema)

    def _discover_schema(self) -> Dict[str, Any]:
        db = self._connect()
        collections = []
        for name in db.list_collection_names():
            sample = list(db[name].find().limit(5))
            fields: Dict[str, str] = {}
            for doc in sample:
                for k, v in doc.items():
                    if k not in fields:
                        fields[k] = type(v).__name__
            collections.append(
                {
                    "name": name,
                    "document_count": db[name].estimated_document_count(),
                    "fields": [{"name": k, "type": v} for k, v in fields.items()],
                }
            )
        return {"collections": collections}

    def close(self) -> None:
        """Close the MongoDB connection."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
