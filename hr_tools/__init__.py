"""
HR tool package (filesystem path: hr_tools/).

Named ``hr_tools`` (not ``tools``) so we never shadow Hermes Agent's
top-level ``tools`` package (``tools.registry``, etc.).
"""

from .employee_service import EmployeeService, get_employee_service
from .employee_tool import TOOL_SCHEMAS, get_tool_handlers, register_hr_tools

__all__ = [
    "EmployeeService",
    "get_employee_service",
    "TOOL_SCHEMAS",
    "get_tool_handlers",
    "register_hr_tools",
]
