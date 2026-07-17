"""
HR tool package (filesystem path: tools/).

Imported as ``hr_tools`` via pyproject package-dir mapping so we do not
shadow Hermes Agent's own top-level ``tools`` package.
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
