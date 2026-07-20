"""
Hermes-compatible SQL tools for the PostgreSQL SQL Agent.

Langflow / LangChain SQLAgent-style toolkit (toolset name: ``sql``).

Tool call count and order are **dynamic** (chosen by the LLM per question):

  * list_tables      ≈ SQL DB LIST TABLES
  * describe_table   ≈ SQL DB SCHEMA (one table)
  * sql_db_schema    ≈ ACCESSING SQL DB SCHEMA (one or many tables)
  * check_sql        ≈ SQL DB QUERY CHECKER
  * run_sql          ≈ SQL DB QUERY
  * db_ping

Each handler returns a JSON **string** (Hermes contract).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from hr_tools.db_service import get_database_service

logger = logging.getLogger("sql_tool")

TOOLSET_NAME = "sql"

# UI-friendly labels (Langflow-style chips)
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "list_tables": "SQL DB LIST TABLES",
    "describe_table": "SQL DB SCHEMA",
    "sql_db_schema": "ACCESSING SQL DB SCHEMA",
    "check_sql": "SQL DB QUERY CHECKER",
    "run_sql": "SQL DB QUERY",
    "db_ping": "SQL DB PING",
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps(
        {"success": False, "error": message, **extra},
        ensure_ascii=False,
        default=str,
    )


def _svc():
    return get_database_service()


def _parse_table_list(raw: Any) -> list[str]:
    """Accept list, comma/space separated string, or single name."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip() for x in raw if str(x).strip()]
    else:
        text = str(raw).strip()
        if not text:
            return []
        names = [p.strip() for p in re.split(r"[,;\s]+", text) if p.strip()]
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def list_tables(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """SQL DB LIST TABLES — list tables/views in a schema (default: public)."""
    del kwargs
    args = args or {}
    try:
        schema = (args.get("schema") or "public").strip()
        include_views = args.get("include_views")
        if include_views is None:
            include_views = True
        tables = _svc().list_tables(schema=schema, include_views=bool(include_views))
        if not tables:
            return _ok(
                message="No tables found",
                schema=schema,
                count=0,
                tables=[],
            )
        return _ok(schema=schema, count=len(tables), tables=tables)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_tables failed")
        return _err(str(exc))


def describe_table(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """SQL DB SCHEMA — columns, PK, FK for one table."""
    del kwargs
    args = args or {}
    try:
        table_name = args.get("table_name") or args.get("table")
        if not table_name:
            return _err("table_name is required")
        schema = (args.get("schema") or "public").strip()
        detail = _svc().describe_table(str(table_name), schema=schema)
        if not detail.get("found"):
            return _ok(
                message=detail.get("message", "No data found"),
                found=False,
                schema=schema,
                table_name=table_name,
                columns=[],
            )
        return _ok(**detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("describe_table failed")
        return _err(str(exc))


def sql_db_schema(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """
    ACCESSING SQL DB SCHEMA — load schema for one or many tables in one call.

    Langflow/LangChain-style: pass table names after list_tables when you need
    several schemas at once (employees, work_places, positions, …).
    """
    del kwargs
    args = args or {}
    try:
        names = _parse_table_list(
            args.get("tables")
            or args.get("table_names")
            or args.get("table_name")
            or args.get("table")
        )
        schema = (args.get("schema") or "public").strip()
        if not names:
            return _err(
                "tables is required — comma-separated list e.g. "
                "'employees, work_places, positions'"
            )
        tables_out: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in names:
            detail = _svc().describe_table(name, schema=schema)
            if not detail.get("found"):
                missing.append(name)
                tables_out.append(
                    {
                        "found": False,
                        "table_name": name,
                        "schema": schema,
                        "message": detail.get("message"),
                    }
                )
            else:
                tables_out.append(detail)
        return _ok(
            schema=schema,
            requested=names,
            table_count=len(names),
            found_count=len(names) - len(missing),
            missing=missing,
            tables=tables_out,
            message=(
                "Schema loaded for requested tables. "
                "Call again with other tables if needed."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("sql_db_schema failed")
        return _err(str(exc))


def check_sql(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """
    SQL DB QUERY CHECKER — validate a planned query before run_sql.

    Static read-only rules + optional EXPLAIN. Does not return data rows.
    """
    del kwargs
    args = args or {}
    try:
        sql = args.get("sql") or args.get("query")
        if not sql:
            return _err("sql is required")
        with_explain = args.get("with_explain")
        if with_explain is None:
            with_explain = True
        result = _svc().check_sql(str(sql), with_explain=bool(with_explain))
        if not result.get("valid", False):
            return _err(
                result.get("message") or result.get("explain_error") or "SQL invalid",
                **{k: v for k, v in result.items() if k not in {"message"}},
            )
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_sql failed")
        return _err(str(exc))


def run_sql(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """SQL DB QUERY — execute a single read-only SQL query and return rows."""
    del kwargs
    args = args or {}
    try:
        sql = args.get("sql") or args.get("query")
        if not sql:
            return _err("sql is required")
        max_rows = args.get("max_rows")
        if max_rows is not None and max_rows != "":
            try:
                max_rows_i: int | None = int(max_rows)
            except (TypeError, ValueError):
                return _err("max_rows must be an integer")
        else:
            max_rows_i = None
        result = _svc().execute_readonly(str(sql), max_rows=max_rows_i)
        if result.get("row_count", 0) == 0:
            result = {
                **result,
                "message": "No matching data exists (query returned 0 rows)",
            }
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_sql failed")
        return _err(str(exc))


def db_ping(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Connectivity / readiness check for operators."""
    del kwargs, args
    try:
        readiness = _svc().readiness()
        if readiness.get("ready"):
            return _ok(**readiness)
        return _err(
            readiness.get("error") or "Database not ready",
            **{k: v for k, v in readiness.items() if k != "error"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("db_ping failed")
        return _err(str(exc))


# =============================================================================
# Schemas (OpenAI / Hermes tool schema format)
# =============================================================================

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "SQL DB LIST TABLES — list tables/views in PostgreSQL "
            "(default schema: public). Usually call first when discovering the DB. "
            "Optional on follow-up turns if you already know table names. "
            "Never invent table names. Call count is dynamic (0+ times)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "schema": {
                    "type": "string",
                    "description": "Database schema name (default: public)",
                },
                "include_views": {
                    "type": "boolean",
                    "description": "Include views as well as base tables (default true)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "describe_table",
        "description": (
            "SQL DB SCHEMA — columns/types/PK/FK for ONE table. "
            "Call repeatedly for different tables, or use sql_db_schema for several "
            "at once. Never invent columns. Dynamic: call 0, 1, or many times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Table name (e.g. employees, work_places, positions)",
                },
                "schema": {
                    "type": "string",
                    "description": "Schema name (default: public)",
                },
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "sql_db_schema",
        "description": (
            "ACCESSING SQL DB SCHEMA — load DDL-like schema for one OR many tables "
            "in a single call (Langflow-style). Prefer this when you already know "
            "which tables you need after list_tables, e.g. tables="
            "'employees, work_places, departments'. You may call again with other tables."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tables": {
                    "type": "string",
                    "description": (
                        "Comma-separated table names, e.g. "
                        "'employees, work_places, positions'"
                    ),
                },
                "schema": {
                    "type": "string",
                    "description": "Schema name (default: public)",
                },
            },
            "required": ["tables"],
        },
    },
    {
        "name": "check_sql",
        "description": (
            "SQL DB QUERY CHECKER — validate planned SELECT/WITH before run_sql "
            "(read-only rules + EXPLAIN). Call 0+ times; if invalid, fix and check again. "
            "Does not return data rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL you plan to execute with run_sql",
                },
                "with_explain": {
                    "type": "boolean",
                    "description": "Run EXPLAIN after static checks (default true)",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "SQL DB QUERY — execute read-only SQL and return rows. "
            "Prefer check_sql first. May run multiple queries in one conversation "
            "(explore → refine → final). Never INSERT/UPDATE/DELETE/DDL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single read-only SQL statement",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Max rows to return (default 200, hard cap 1000)",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "db_ping",
        "description": (
            "SQL DB PING — connectivity only if connection errors occur. "
            "Usually not needed for normal questions."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

_HANDLERS: dict[str, Callable[..., str]] = {
    "list_tables": list_tables,
    "describe_table": describe_table,
    "sql_db_schema": sql_db_schema,
    "check_sql": check_sql,
    "run_sql": run_sql,
    "db_ping": db_ping,
}


def get_tool_handlers() -> dict[str, Callable[..., str]]:
    return dict(_HANDLERS)


def register_sql_tools() -> list[str]:
    """
    Register SQL tools with Hermes tools.registry if available.
    Safe when Hermes is absent (returns []).
    """
    try:
        from tools.registry import registry  # type: ignore[import-not-found]
    except Exception:
        logger.warning(
            "Hermes tools.registry not available — SQL tools will be used "
            "via plugin / direct handlers only"
        )
        return []

    registered: list[str] = []
    schema_by_name = {s["name"]: s for s in TOOL_SCHEMAS}

    for name, handler in _HANDLERS.items():
        schema = schema_by_name.get(name)
        if not schema:
            continue

        def _make_handler(h: Callable[..., str]) -> Callable[..., str]:
            def _wrapped(args: dict[str, Any] | None = None, **kw: Any) -> str:
                return h(args or {}, **kw)

            return _wrapped

        registry.register(
            name=name,
            toolset=TOOLSET_NAME,
            schema=schema,
            handler=_make_handler(handler),
            description=schema.get("description", name),
        )
        registered.append(name)

    logger.info("Registered SQL tools with Hermes registry: %s", registered)
    return registered
