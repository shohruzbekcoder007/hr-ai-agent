#!/usr/bin/env python3
"""Validate employees.json and exercise EmployeeService without Hermes/LLM."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load modules by path to avoid package install during quick checks
import importlib.util


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    data_path = ROOT / "data" / "employees.json"
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    employees = raw["employees"] if isinstance(raw, dict) else raw
    assert len(employees) >= 20, f"Need >=20 employees, got {len(employees)}"
    ids = [e["employee_id"] for e in employees]
    assert len(ids) == len(set(ids)), "Duplicate employee_id values"

    import types

    svc_mod = _load("hr_tools.employee_service", ROOT / "tools" / "employee_service.py")
    pkg = types.ModuleType("hr_tools")
    pkg.employee_service = svc_mod  # type: ignore[attr-defined]
    sys.modules["hr_tools"] = pkg
    sys.modules["hr_tools.employee_service"] = svc_mod

    service = svc_mod.EmployeeService(data_path)
    assert service.readiness()["ready"] is True
    assert service.count_employees()["count"] == len(employees)
    assert service.search_employee(skill="Python")
    assert service.search_language(language="English")
    stats = service.salary_statistics()
    assert stats["salary_summary"]["average"] is not None
    depts = service.list_departments()
    assert len(depts) >= 3

    print("OK — employees:", len(employees))
    print("OK — departments:", len(depts))
    print("OK — avg salary:", stats["salary_summary"]["average"])
    print("OK — readiness:", service.readiness())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
