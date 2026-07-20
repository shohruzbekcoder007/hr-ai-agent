"""
PostgreSQL access layer for the Hermes SQL Agent.

Connection string: DATABASE_URL (or POSTGRES_URL / HR_DATABASE_URL).
Read-only usage is enforced at the tool layer; prefer a readonly DB role.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Generator, Optional
from urllib.parse import urlparse

logger = logging.getLogger("sql_tool")

_service: Optional["DatabaseService"] = None
_service_lock = threading.RLock()

# Statements / keywords that must never run through run_sql
_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|"
    r"GRANT|REVOKE|COMMENT|COPY|CALL|DO|EXECUTE|PERFORM|"
    r"VACUUM|ANALYZE|REINDEX|CLUSTER|REFRESH|SECURITY|OWNER|"
    r"SET\s+ROLE|SET\s+SESSION|RESET\s+ROLE|"
    r"pg_sleep|lo_import|lo_export"
    r")\b",
    re.IGNORECASE,
)

_MULTI_STATEMENT = re.compile(r";\s*\S", re.DOTALL)


def resolve_database_url(explicit: str | None = None) -> str | None:
    """Return the first non-empty DB URL from explicit arg or env."""
    if explicit and explicit.strip():
        return explicit.strip()
    for key in ("DATABASE_URL", "HR_DATABASE_URL", "POSTGRES_URL"):
        raw = os.getenv(key, "").strip()
        if raw:
            return raw
    return None


def _redact_url(url: str) -> str:
    """Hide password in logs."""
    try:
        p = urlparse(url)
        if p.password is None:
            return url
        netloc = p.netloc.replace(f":{p.password}@", ":***@")
        return p._replace(netloc=netloc).geturl()
    except Exception:  # noqa: BLE001
        return "<unparseable DATABASE_URL>"


def validate_readonly_sql(sql: str) -> str:
    """
    Normalize and validate that SQL is a single read-only query.

    Returns cleaned SQL (trailing semicolon stripped).
    Raises ValueError on rejection.
    """
    if not sql or not str(sql).strip():
        raise ValueError("SQL must not be empty")

    cleaned = str(sql).strip()
    # Strip one trailing semicolon
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()

    # Remove block/line comments for keyword scan (best-effort)
    no_block = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    no_line = re.sub(r"--.*?$", " ", no_block, flags=re.MULTILINE)
    scan = no_line.strip()

    if not scan:
        raise ValueError("SQL is empty after stripping comments")

    if _MULTI_STATEMENT.search(cleaned):
        raise ValueError("Only a single SQL statement is allowed")

    if _FORBIDDEN.search(scan):
        raise ValueError(
            "Only read-only SELECT/WITH queries are allowed "
            "(no INSERT/UPDATE/DELETE/DDL/admin statements)"
        )

    # Must start with SELECT or WITH (CTE)
    head = re.match(r"^\s*(\w+)", scan, re.IGNORECASE)
    if not head or head.group(1).upper() not in {"SELECT", "WITH", "TABLE", "VALUES", "SHOW", "EXPLAIN"}:
        raise ValueError(
            "Query must start with SELECT, WITH, TABLE, VALUES, SHOW, or EXPLAIN"
        )

    # EXPLAIN is allowed for planning; EXPLAIN ANALYZE can write side effects — block ANALYZE form
    if re.match(r"^\s*EXPLAIN\s+ANALYZE\b", scan, re.IGNORECASE):
        raise ValueError("EXPLAIN ANALYZE is not allowed; use EXPLAIN without ANALYZE")

    return cleaned


class DatabaseService:
    """Thread-safe PostgreSQL helper using psycopg (v3) connection pool."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = resolve_database_url(database_url)
        self._lock = threading.RLock()
        self._pool: Any = None
        self._last_error: str | None = None
        self._connected = False

        if self.database_url:
            try:
                self._open_pool()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("Database pool not opened at init: %s", exc)

    def _open_pool(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Set DATABASE_URL "
                "(postgresql://user:pass@host:5432/dbname) and restart."
            )
        try:
            from psycopg_pool import ConnectionPool  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "psycopg_pool is required. Install: pip install 'psycopg[binary,pool]'"
            ) from exc

        min_size = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
        max_size = int(os.getenv("DB_POOL_MAX_SIZE", "5"))
        kwargs: dict[str, Any] = {
            "conninfo": self.database_url,
            "min_size": max(1, min_size),
            "max_size": max(min_size, max_size),
            "kwargs": {
                "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
                "options": f"-c statement_timeout={int(os.getenv('DB_STATEMENT_TIMEOUT_MS', '30000'))}",
            },
        }
        self._pool = ConnectionPool(**kwargs)
        # Probe
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        self._connected = True
        self._last_error = None
        logger.info(
            "PostgreSQL pool ready (%s)",
            _redact_url(self.database_url),
        )

    def ensure_connected(self) -> None:
        with self._lock:
            if self._connected and self._pool is not None:
                return
            self._open_pool()

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Error closing DB pool")
                self._pool = None
            self._connected = False

    def readiness(self) -> dict[str, Any]:
        url_set = bool(self.database_url)
        ready = False
        error = self._last_error
        server_version: str | None = None
        if url_set:
            try:
                self.ensure_connected()
                with self.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version")
                        row = cur.fetchone()
                        server_version = str(row[0]) if row else None
                ready = True
                error = None
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                ready = False
                self._last_error = error
        return {
            "ready": ready,
            "database_url_configured": url_set,
            "database_url_redacted": _redact_url(self.database_url) if self.database_url else None,
            "connected": self._connected,
            "server_version": server_version,
            "error": error,
            "knowledge_source": "postgresql",
        }

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        self.ensure_connected()
        assert self._pool is not None
        with self._pool.connection() as conn:
            yield conn

    def list_tables(
        self,
        *,
        schema: str = "public",
        include_views: bool = True,
    ) -> list[dict[str, Any]]:
        types = ("BASE TABLE", "VIEW") if include_views else ("BASE TABLE",)
        sql = """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = ANY(%s)
            ORDER BY table_name
        """
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (schema, list(types)))
                rows = cur.fetchall()
        return [
            {
                "table_schema": r[0],
                "table_name": r[1],
                "table_type": r[2],
            }
            for r in rows
        ]

    def describe_table(
        self,
        table_name: str,
        *,
        schema: str = "public",
    ) -> dict[str, Any]:
        table_name = (table_name or "").strip()
        schema = (schema or "public").strip()
        if not table_name:
            raise ValueError("table_name is required")
        # Allow only simple identifiers
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
            raise ValueError("Invalid table_name identifier")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", schema):
            raise ValueError("Invalid schema identifier")

        col_sql = """
            SELECT
                column_name,
                data_type,
                udt_name,
                is_nullable,
                column_default,
                character_maximum_length,
                numeric_precision,
                numeric_scale,
                ordinal_position
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """
        pk_sql = """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
        """
        fk_sql = """
            SELECT
                kcu.column_name,
                ccu.table_schema AS foreign_table_schema,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name,
                tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'FOREIGN KEY'
        """
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(col_sql, (schema, table_name))
                cols = cur.fetchall()
                if not cols:
                    return {
                        "found": False,
                        "schema": schema,
                        "table_name": table_name,
                        "message": f"No table/view named {schema}.{table_name}",
                        "columns": [],
                    }
                cur.execute(pk_sql, (schema, table_name))
                pks = [r[0] for r in cur.fetchall()]
                cur.execute(fk_sql, (schema, table_name))
                fks = [
                    {
                        "column": r[0],
                        "references": f"{r[1]}.{r[2]}({r[3]})",
                        "constraint": r[4],
                    }
                    for r in cur.fetchall()
                ]

        columns = [
            {
                "name": r[0],
                "data_type": r[1],
                "udt_name": r[2],
                "is_nullable": r[3],
                "column_default": r[4],
                "character_maximum_length": r[5],
                "numeric_precision": r[6],
                "numeric_scale": r[7],
                "ordinal_position": r[8],
            }
            for r in cols
        ]
        return {
            "found": True,
            "schema": schema,
            "table_name": table_name,
            "columns": columns,
            "primary_key": pks,
            "foreign_keys": fks,
        }

    def check_sql(self, sql: str, *, with_explain: bool = True) -> dict[str, Any]:
        """
        Query checker: validate read-only policy and optionally EXPLAIN the plan.

        Does not return result rows — use execute_readonly / run_sql for data.
        """
        cleaned = validate_readonly_sql(sql)
        out: dict[str, Any] = {
            "valid": True,
            "sql": cleaned,
            "read_only_ok": True,
            "checks": [
                "single_statement",
                "select_or_with_only",
                "no_mutating_keywords",
            ],
            "message": "SQL passed static read-only checks",
        }
        if not with_explain:
            return out

        # Skip EXPLAIN of statements that already start with EXPLAIN
        scan = cleaned.lstrip()
        if re.match(r"^(EXPLAIN|SHOW)\b", scan, re.IGNORECASE):
            out["explain"] = None
            out["message"] = "SQL passed static checks (EXPLAIN skipped for EXPLAIN/SHOW)"
            return out

        explain_sql = f"EXPLAIN (FORMAT TEXT) {cleaned}"
        try:
            with self.connection() as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(explain_sql)
                    plan_rows = cur.fetchall()
            out["explain"] = [str(r[0]) for r in plan_rows]
            out["message"] = "SQL passed static checks and EXPLAIN succeeded"
        except Exception as exc:  # noqa: BLE001
            out["valid"] = False
            out["explain_error"] = str(exc)
            out["message"] = (
                "Static checks passed but EXPLAIN failed — fix the SQL before run_sql"
            )
        return out

    def execute_readonly(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        cleaned = validate_readonly_sql(sql)
        limit = max_rows
        if limit is None:
            limit = int(os.getenv("DB_MAX_ROWS", "200"))
        limit = max(1, min(int(limit), int(os.getenv("DB_MAX_ROWS_HARD_CAP", "1000"))))

        with self.connection() as conn:
            # Ensure we never commit writes even if validation missed something
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(cleaned)
                if cur.description is None:
                    return {
                        "sql": cleaned,
                        "columns": [],
                        "rows": [],
                        "row_count": 0,
                        "truncated": False,
                        "message": "Query produced no result set",
                    }
                columns = [d.name for d in cur.description]
                raw_rows = cur.fetchmany(limit + 1)
                truncated = len(raw_rows) > limit
                raw_rows = raw_rows[:limit]
                rows = [
                    {
                        columns[i]: _jsonable(cell)
                        for i, cell in enumerate(row)
                    }
                    for row in raw_rows
                ]
        return {
            "sql": cleaned,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": limit,
        }


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    # date/datetime/Decimal/UUID etc.
    return str(value)


def get_database_service(database_url: str | None = None) -> DatabaseService:
    """Process-wide DatabaseService singleton.

    Re-creates the pool if a new URL appears (e.g. env set after first import).
    """
    global _service
    with _service_lock:
        resolved = resolve_database_url(database_url)
        if _service is None:
            _service = DatabaseService(resolved)
            return _service
        # Hot-pick up URL when it was previously missing or changed
        if resolved and resolved != _service.database_url:
            _service.close()
            _service = DatabaseService(resolved)
        return _service
