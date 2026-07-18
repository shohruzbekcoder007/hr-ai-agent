"""
Hermes-compatible HR tools for the employee JSON knowledge base.

Each handler returns a JSON **string** (Hermes contract). Tools can be:
  1. Registered via the Hermes plugin (`plugins/hr-employee`)
  2. Registered at runtime with `register_hr_tools()` against tools.registry
  3. Invoked directly by tests / CLI without Hermes
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hr_tools.employee_service import EmployeeService, get_employee_service

logger = logging.getLogger("employee_tool")

TOOLSET_NAME = "hr"


def _ok(**payload: Any) -> str:
    body = {"success": True, **payload}
    return json.dumps(body, ensure_ascii=False, default=str)


def _err(message: str, **extra: Any) -> str:
    body = {"success": False, "error": message, **extra}
    return json.dumps(body, ensure_ascii=False, default=str)


def _svc() -> EmployeeService:
    return get_employee_service()


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =============================================================================
# Handlers
# =============================================================================


def search_employee(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Search employees with flexible filters."""
    del kwargs
    args = args or {}
    try:
        rows = _svc().search_employee(
            employee_id=args.get("employee_id"),
            name=args.get("name"),
            first_name=args.get("first_name"),
            last_name=args.get("last_name"),
            department=args.get("department"),
            position=args.get("position"),
            city=args.get("city"),
            country=args.get("country"),
            status=args.get("status"),
            manager_id=args.get("manager_id"),
            skill=args.get("skill"),
            language=args.get("language"),
            hired_after=args.get("hired_after"),
            hired_before=args.get("hired_before"),
            older_than=_optional_int(args.get("older_than")),
            younger_than=_optional_int(args.get("younger_than")),
            min_salary=_optional_float(args.get("min_salary")),
            max_salary=_optional_float(args.get("max_salary")),
            min_experience=_optional_float(args.get("min_experience")),
            query=args.get("query"),
            limit=_optional_int(args.get("limit")) or 50,
            include_sensitive=bool(args.get("include_sensitive", True)),
        )
        if not rows:
            return _ok(message="No data found", count=0, employees=[])
        return _ok(count=len(rows), employees=rows)
    except Exception as exc:  # noqa: BLE001 — tools must not raise
        logger.exception("search_employee failed")
        return _err(str(exc))


def list_employees(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """List employees, optionally filtered by status."""
    del kwargs
    args = args or {}
    try:
        rows = _svc().all_employees(
            status=args.get("status"),
            include_sensitive=bool(args.get("include_sensitive", True)),
        )
        if not rows:
            return _ok(message="No data found", count=0, employees=[])
        return _ok(count=len(rows), employees=rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_employees failed")
        return _err(str(exc))


def get_employee(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Fetch a single employee by ID."""
    del kwargs
    args = args or {}
    employee_id = (args.get("employee_id") or "").strip()
    if not employee_id:
        return _err("employee_id is required")
    try:
        emp = _svc().get_by_id(
            employee_id,
            include_sensitive=bool(args.get("include_sensitive", True)),
        )
        if not emp:
            return _ok(message="No data found", found=False, employee=None)
        return _ok(found=True, employee=emp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_employee failed")
        return _err(str(exc))


def find_department(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Find everyone in a department."""
    del kwargs
    args = args or {}
    department = (args.get("department") or "").strip()
    if not department:
        return _err("department is required")
    try:
        result = _svc().find_department(
            department,
            status=args.get("status", "active"),
            include_sensitive=bool(args.get("include_sensitive", True)),
        )
        if result["count"] == 0:
            return _ok(message="No data found", **result)
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("find_department failed")
        return _err(str(exc))


def list_departments(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """List all departments with headcount and salary summaries."""
    del kwargs, args
    try:
        departments = _svc().list_departments()
        if not departments:
            return _ok(message="No data found", count=0, departments=[])
        return _ok(count=len(departments), departments=departments)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_departments failed")
        return _err(str(exc))


def search_skill(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Find employees with a given skill (substring match)."""
    del kwargs
    args = args or {}
    skill = (args.get("skill") or "").strip()
    if not skill:
        return _err("skill is required")
    try:
        rows = _svc().search_skill(skill, status=args.get("status"))
        if not rows:
            return _ok(message="No data found", count=0, skill=skill, employees=[])
        return _ok(count=len(rows), skill=skill, employees=rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("search_skill failed")
        return _err(str(exc))


def search_language(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Find employees speaking a language."""
    del kwargs
    args = args or {}
    language = (args.get("language") or "").strip()
    if not language:
        return _err("language is required")
    try:
        rows = _svc().search_language(language, status=args.get("status"))
        if not rows:
            return _ok(
                message="No data found",
                count=0,
                language=language,
                employees=[],
            )
        return _ok(count=len(rows), language=language, employees=rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("search_language failed")
        return _err(str(exc))


def salary_statistics(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Salary aggregates (avg/min/max/highest/lowest)."""
    del kwargs
    args = args or {}
    try:
        stats = _svc().salary_statistics(
            department=args.get("department"),
            status=args.get("status", "active"),
        )
        if stats["employee_count"] == 0:
            return _ok(message="No data found", **stats)
        return _ok(**stats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("salary_statistics failed")
        return _err(str(exc))


def employee_statistics(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Organization-wide statistics."""
    del kwargs, args
    try:
        stats = _svc().employee_statistics()
        if stats.get("total_employees", 0) == 0:
            return _ok(message="No data found", **stats)
        return _ok(**stats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("employee_statistics failed")
        return _err(str(exc))


def count_employees(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Count employees, optionally by status."""
    del kwargs
    args = args or {}
    try:
        result = _svc().count_employees(status=args.get("status"))
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("count_employees failed")
        return _err(str(exc))


def get_manager_chain(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Resolve manager reporting chain for an employee."""
    del kwargs
    args = args or {}
    employee_id = (args.get("employee_id") or "").strip()
    if not employee_id:
        return _err("employee_id is required")
    try:
        result = _svc().get_manager_chain(employee_id)
        if not result.get("found"):
            return _ok(message="No data found", **result)
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_manager_chain failed")
        return _err(str(exc))


def reload_employees(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Hot-reload employees.json from disk (ops / admin)."""
    del kwargs, args
    try:
        result = _svc().reload()
        return _ok(**result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("reload_employees failed")
        return _err(str(exc))


# =============================================================================
# Schemas (OpenAI / Hermes tool schema format)
# =============================================================================

_FILTER_PROPERTIES: dict[str, Any] = {
    "employee_id": {
        "type": "string",
        "description": "Exact employee ID (e.g. EMP-001)",
    },
    "name": {
        "type": "string",
        "description": "Partial match against first/middle/last/full name",
    },
    "first_name": {"type": "string", "description": "Partial first name match"},
    "last_name": {"type": "string", "description": "Partial last name match"},
    "department": {
        "type": "string",
        "description": "Department name substring (e.g. Engineering, HR)",
    },
    "position": {
        "type": "string",
        "description": "Job title / position substring (e.g. programmer, developer)",
    },
    "city": {"type": "string", "description": "City substring"},
    "country": {"type": "string", "description": "Country substring"},
    "status": {
        "type": "string",
        "description": "Employment status filter (active, on_leave, terminated)",
    },
    "manager_id": {
        "type": "string",
        "description": "Manager employee_id, or 'null' for top-level",
    },
    "skill": {"type": "string", "description": "Skill substring (e.g. Python)"},
    "language": {
        "type": "string",
        "description": "Language substring (e.g. English)",
    },
    "hired_after": {
        "type": "string",
        "description": "ISO date YYYY-MM-DD — hire_date >= this",
    },
    "hired_before": {
        "type": "string",
        "description": "ISO date YYYY-MM-DD — hire_date <= this",
    },
    "older_than": {
        "type": "integer",
        "description": "Include only employees older than this age (years)",
    },
    "younger_than": {
        "type": "integer",
        "description": "Include only employees younger than this age (years)",
    },
    "min_salary": {"type": "number", "description": "Minimum salary inclusive"},
    "max_salary": {"type": "number", "description": "Maximum salary inclusive"},
    "min_experience": {
        "type": "number",
        "description": "Minimum years of experience",
    },
    "query": {
        "type": "string",
        "description": "Free-text match across name, dept, position, skills, etc.",
    },
    "limit": {
        "type": "integer",
        "description": "Max results to return (default 50, max 500)",
    },
    "include_sensitive": {
        "type": "boolean",
        "description": "Include salary and contact fields (default true for HR)",
    },
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_employee",
        "description": (
            "Search the employee directory with flexible filters "
            "(name, ID, department, position, skill, language, hire date, age, salary). "
            "Use this for almost any employee lookup question."
        ),
        "parameters": {
            "type": "object",
            "properties": _FILTER_PROPERTIES,
            "required": [],
        },
    },
    {
        "name": "list_employees",
        "description": "List all employees in the directory. Optionally filter by status.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter (e.g. active)",
                },
                "include_sensitive": {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "get_employee",
        "description": "Get full details for a single employee by employee_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "employee_id": {
                    "type": "string",
                    "description": "Employee ID (e.g. EMP-005)",
                },
                "include_sensitive": {"type": "boolean"},
            },
            "required": ["employee_id"],
        },
    },
    {
        "name": "find_department",
        "description": (
            "Find all employees in a department and return a short salary summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "department": {
                    "type": "string",
                    "description": "Department name (e.g. HR, Engineering)",
                },
                "status": {
                    "type": "string",
                    "description": "Default: active",
                },
                "include_sensitive": {"type": "boolean"},
            },
            "required": ["department"],
        },
    },
    {
        "name": "list_departments",
        "description": (
            "List all departments with headcount, positions, and salary statistics."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_skill",
        "description": "Find employees who have a skill (e.g. Python, Docker, Recruiting).",
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill to search for"},
                "status": {"type": "string"},
            },
            "required": ["skill"],
        },
    },
    {
        "name": "search_language",
        "description": "Find employees who speak a language (e.g. English, German).",
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "Language to search for",
                },
                "status": {"type": "string"},
            },
            "required": ["language"],
        },
    },
    {
        "name": "salary_statistics",
        "description": (
            "Compute salary statistics (min, max, average, highest earner, lowest earner). "
            "Optionally scope to a department."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "department": {
                    "type": "string",
                    "description": "Optional department filter",
                },
                "status": {
                    "type": "string",
                    "description": "Default: active",
                },
            },
            "required": [],
        },
    },
    {
        "name": "employee_statistics",
        "description": (
            "Organization-wide statistics: headcount by department/status/country, "
            "age summary, top skills, top languages, salary overview."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "count_employees",
        "description": "Count employees in the directory, optionally filtered by status.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_manager_chain",
        "description": (
            "Resolve the reporting chain for an employee (employee → manager → ... → CEO)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "employee_id": {
                    "type": "string",
                    "description": "Employee ID to resolve",
                },
            },
            "required": ["employee_id"],
        },
    },
    {
        "name": "reload_employees",
        "description": (
            "Reload employees.json from disk after an external update. "
            "Use only when the user or operator requests a data refresh."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

_HANDLERS: dict[str, Callable[..., str]] = {
    "search_employee": search_employee,
    "list_employees": list_employees,
    "get_employee": get_employee,
    "find_department": find_department,
    "list_departments": list_departments,
    "search_skill": search_skill,
    "search_language": search_language,
    "salary_statistics": salary_statistics,
    "employee_statistics": employee_statistics,
    "count_employees": count_employees,
    "get_manager_chain": get_manager_chain,
    "reload_employees": reload_employees,
}


def get_tool_handlers() -> dict[str, Callable[..., str]]:
    """Return a copy of name → handler mapping."""
    return dict(_HANDLERS)


def register_hr_tools() -> list[str]:
    """
    Register HR tools with the Hermes tools.registry if available.

    Returns the list of registered tool names. Safe to call multiple times
    (re-registration overwrites). Falls back gracefully if Hermes is absent.
    """
    try:
        # Hermes ships a top-level package named ``tools``. Our package is
        # ``hr_tools/`` so this import resolves to Hermes, not our code.
        from tools.registry import registry  # type: ignore[import-not-found]
    except Exception:
        logger.warning(
            "Hermes tools.registry not available — HR tools will be used via plugin / direct handlers only"
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

    logger.info("Registered %d HR tools into Hermes registry: %s", len(registered), registered)
    return registered
