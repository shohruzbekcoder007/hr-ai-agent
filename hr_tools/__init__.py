"""
SQL agent tool package (filesystem path: hr_tools/).

Named ``hr_tools`` (not ``tools``) so we never shadow Hermes Agent's
top-level ``tools`` package (``tools.registry``, etc.).

Knowledge source: PostgreSQL via DatabaseService + SQL tools only.
There is no employees.json path.
"""

from .db_service import DatabaseService, get_database_service
from .sql_tool import TOOL_SCHEMAS, get_tool_handlers, register_sql_tools

__all__ = [
    "DatabaseService",
    "get_database_service",
    "TOOL_SCHEMAS",
    "get_tool_handlers",
    "register_sql_tools",
]
