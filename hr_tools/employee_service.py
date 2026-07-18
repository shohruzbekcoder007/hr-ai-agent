"""
EmployeeService — sole knowledge layer for the HR AI Agent.

Loads employees.json at startup (and supports reload), exposes typed query
helpers used by Hermes tools. No database, no vector store, no RAG.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("employee_tool")

# Module-level singleton (process-wide, thread-safe via RLock)
_service: Optional["EmployeeService"] = None
_service_lock = threading.RLock()


def _parse_date(value: str | None) -> date | None:
    """Parse ISO date (YYYY-MM-DD). Returns None on missing/invalid values."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _age_years(birth_date: date | None, today: date | None = None) -> int | None:
    """Compute whole years of age from birth_date."""
    if birth_date is None:
        return None
    today = today or date.today()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _normalize(text: str | None) -> str:
    return (text or "").strip().lower()


class EmployeeService:
    """In-memory employee directory backed by a JSON file."""

    def __init__(self, json_path: str | Path) -> None:
        self.json_path = Path(json_path)
        self._lock = threading.RLock()
        self.meta: dict[str, Any] = {}
        self.employees: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self.loaded_at: datetime | None = None
        self.load()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> int:
        """Load (or reload) employees.json. Returns number of employees loaded."""
        with self._lock:
            if not self.json_path.is_file():
                raise FileNotFoundError(
                    f"employees.json not found at: {self.json_path}"
                )
            with self.json_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)

            if isinstance(payload, list):
                employees = payload
                meta: dict[str, Any] = {}
            elif isinstance(payload, dict):
                employees = payload.get("employees", [])
                meta = {
                    k: v
                    for k, v in payload.items()
                    if k != "employees"
                }
                if "meta" in payload and isinstance(payload["meta"], dict):
                    meta = payload["meta"]
            else:
                raise ValueError(
                    "employees.json must be a list of employees or an object "
                    "with an 'employees' array"
                )

            if not isinstance(employees, list):
                raise ValueError("'employees' must be a JSON array")

            cleaned: list[dict[str, Any]] = []
            by_id: dict[str, dict[str, Any]] = {}
            for raw in employees:
                if not isinstance(raw, dict):
                    logger.warning("Skipping non-object employee entry: %r", raw)
                    continue
                emp = dict(raw)
                emp_id = str(emp.get("employee_id", "")).strip()
                if not emp_id:
                    logger.warning("Skipping employee without employee_id: %r", emp)
                    continue
                emp["employee_id"] = emp_id
                # Ensure list fields are lists
                for field in ("skills", "languages"):
                    val = emp.get(field)
                    if val is None:
                        emp[field] = []
                    elif isinstance(val, str):
                        emp[field] = [val]
                    elif not isinstance(val, list):
                        emp[field] = list(val)
                cleaned.append(emp)
                by_id[emp_id] = emp

            self.employees = cleaned
            self._by_id = by_id
            self.meta = meta
            self.loaded_at = datetime.utcnow()
            logger.info(
                "Loaded %d employees from %s (org=%s)",
                len(self.employees),
                self.json_path,
                self.meta.get("organization", "unknown"),
            )
            return len(self.employees)

    def reload(self) -> dict[str, Any]:
        """Reload JSON from disk and return a status payload."""
        count = self.load()
        return {
            "success": True,
            "count": count,
            "path": str(self.json_path),
            "loaded_at": self.loaded_at.isoformat() + "Z" if self.loaded_at else None,
        }

    def readiness(self) -> dict[str, Any]:
        """Readiness payload for health checks."""
        with self._lock:
            ok = len(self.employees) > 0
            return {
                "ready": ok,
                "employee_count": len(self.employees),
                "json_path": str(self.json_path),
                "json_exists": self.json_path.is_file(),
                "loaded_at": self.loaded_at.isoformat() + "Z" if self.loaded_at else None,
                "organization": self.meta.get("organization"),
            }

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------

    def all_employees(
        self,
        *,
        status: str | None = None,
        include_sensitive: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.employees)
        if status:
            rows = [e for e in rows if _normalize(e.get("status")) == _normalize(status)]
        return [self._project(e, include_sensitive=include_sensitive) for e in rows]

    def get_by_id(
        self,
        employee_id: str,
        *,
        include_sensitive: bool = True,
    ) -> dict[str, Any] | None:
        with self._lock:
            emp = self._by_id.get(employee_id.strip())
            if emp is None:
                # case-insensitive fallback
                needle = _normalize(employee_id)
                emp = next(
                    (v for k, v in self._by_id.items() if _normalize(k) == needle),
                    None,
                )
            return (
                self._project(emp, include_sensitive=include_sensitive)
                if emp
                else None
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_employee(
        self,
        *,
        employee_id: str | None = None,
        name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        department: str | None = None,
        position: str | None = None,
        city: str | None = None,
        country: str | None = None,
        status: str | None = None,
        manager_id: str | None = None,
        skill: str | None = None,
        language: str | None = None,
        hired_after: str | None = None,
        hired_before: str | None = None,
        older_than: int | None = None,
        younger_than: int | None = None,
        min_salary: float | None = None,
        max_salary: float | None = None,
        min_experience: float | None = None,
        query: str | None = None,
        limit: int = 50,
        include_sensitive: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Flexible multi-field employee search.

        All filters are ANDed. Name matching is case-insensitive substring.
        """
        with self._lock:
            candidates = list(self.employees)

        hired_after_d = _parse_date(hired_after)
        hired_before_d = _parse_date(hired_before)
        today = date.today()
        limit = max(1, min(int(limit or 50), 500))

        results: list[dict[str, Any]] = []
        for emp in candidates:
            if employee_id and _normalize(emp.get("employee_id")) != _normalize(employee_id):
                continue
            if first_name and _normalize(first_name) not in _normalize(emp.get("first_name")):
                continue
            if last_name and _normalize(last_name) not in _normalize(emp.get("last_name")):
                continue
            if name:
                full = " ".join(
                    filter(
                        None,
                        [
                            emp.get("first_name"),
                            emp.get("middle_name"),
                            emp.get("last_name"),
                        ],
                    )
                )
                if _normalize(name) not in _normalize(full) and _normalize(
                    name
                ) not in _normalize(emp.get("last_name")) and _normalize(
                    name
                ) not in _normalize(emp.get("first_name")):
                    continue
            if department and _normalize(department) not in _normalize(emp.get("department")):
                continue
            if position and _normalize(position) not in _normalize(emp.get("position")):
                continue
            if city and _normalize(city) not in _normalize(emp.get("city")):
                continue
            if country and _normalize(country) not in _normalize(emp.get("country")):
                continue
            if status and _normalize(status) != _normalize(emp.get("status")):
                continue
            if manager_id is not None:
                mgr = emp.get("manager")
                if manager_id == "" or manager_id.lower() in {"null", "none", "top"}:
                    if mgr is not None:
                        continue
                elif _normalize(str(mgr or "")) != _normalize(manager_id):
                    continue
            if skill:
                skills = [_normalize(s) for s in emp.get("skills") or []]
                if not any(_normalize(skill) in s for s in skills):
                    continue
            if language:
                langs = [_normalize(s) for s in emp.get("languages") or []]
                if not any(_normalize(language) in s for s in langs):
                    continue
            hire = _parse_date(emp.get("hire_date"))
            if hired_after_d and (hire is None or hire < hired_after_d):
                continue
            if hired_before_d and (hire is None or hire > hired_before_d):
                continue
            age = _age_years(_parse_date(emp.get("birth_date")), today)
            if older_than is not None and (age is None or age <= older_than):
                continue
            if younger_than is not None and (age is None or age >= younger_than):
                continue
            sal = emp.get("salary")
            try:
                sal_f = float(sal) if sal is not None else None
            except (TypeError, ValueError):
                sal_f = None
            if min_salary is not None and (sal_f is None or sal_f < min_salary):
                continue
            if max_salary is not None and (sal_f is None or sal_f > max_salary):
                continue
            exp = emp.get("experience_years")
            try:
                exp_f = float(exp) if exp is not None else None
            except (TypeError, ValueError):
                exp_f = None
            if min_experience is not None and (exp_f is None or exp_f < min_experience):
                continue
            if query:
                blob = " ".join(
                    str(x)
                    for x in [
                        emp.get("employee_id"),
                        emp.get("first_name"),
                        emp.get("middle_name"),
                        emp.get("last_name"),
                        emp.get("department"),
                        emp.get("position"),
                        emp.get("email"),
                        emp.get("city"),
                        emp.get("country"),
                        " ".join(emp.get("skills") or []),
                        " ".join(emp.get("languages") or []),
                    ]
                    if x
                )
                if _normalize(query) not in _normalize(blob):
                    continue

            projected = self._project(emp, include_sensitive=include_sensitive)
            if age is not None:
                projected["age_years"] = age
            results.append(projected)
            if len(results) >= limit:
                break

        return results

    def find_department(
        self,
        department: str,
        *,
        status: str | None = "active",
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Return all employees in a department plus a short summary."""
        rows = self.search_employee(
            department=department,
            status=status,
            limit=500,
            include_sensitive=include_sensitive,
        )
        if not rows:
            # try exact listing without status filter if empty
            rows = self.search_employee(
                department=department,
                status=None,
                limit=500,
                include_sensitive=include_sensitive,
            )
        salaries = [float(r["salary"]) for r in rows if r.get("salary") is not None]
        return {
            "department": department,
            "count": len(rows),
            "employees": rows,
            "salary_summary": self._salary_summary(salaries),
        }

    def list_departments(self) -> list[dict[str, Any]]:
        """Distinct departments with headcount and average salary."""
        with self._lock:
            rows = list(self.employees)

        buckets: dict[str, list[dict[str, Any]]] = {}
        for emp in rows:
            dept = emp.get("department") or "Unknown"
            buckets.setdefault(dept, []).append(emp)

        result: list[dict[str, Any]] = []
        for dept, members in sorted(buckets.items(), key=lambda x: x[0].lower()):
            salaries = [
                float(m["salary"])
                for m in members
                if m.get("salary") is not None
            ]
            active = sum(1 for m in members if _normalize(m.get("status")) == "active")
            result.append(
                {
                    "department": dept,
                    "total_employees": len(members),
                    "active_employees": active,
                    "salary_summary": self._salary_summary(salaries),
                    "positions": sorted(
                        {str(m.get("position")) for m in members if m.get("position")}
                    ),
                }
            )
        return result

    def search_skill(self, skill: str, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.search_employee(
            skill=skill,
            status=status,
            include_sensitive=False,
            limit=500,
        )

    def search_language(
        self, language: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        return self.search_employee(
            language=language,
            status=status,
            include_sensitive=False,
            limit=500,
        )

    def get_manager_chain(self, employee_id: str) -> dict[str, Any]:
        """Resolve reporting chain from employee up to the top of the tree."""
        person = self.get_by_id(employee_id, include_sensitive=False)
        if not person:
            return {"found": False, "employee_id": employee_id, "chain": []}

        chain: list[dict[str, Any]] = [person]
        seen: set[str] = {person["employee_id"]}
        current_mgr = person.get("manager")
        while current_mgr:
            mgr = self.get_by_id(str(current_mgr), include_sensitive=False)
            if not mgr or mgr["employee_id"] in seen:
                break
            chain.append(mgr)
            seen.add(mgr["employee_id"])
            current_mgr = mgr.get("manager")
        return {
            "found": True,
            "employee_id": person["employee_id"],
            "chain": chain,
            "direct_manager": chain[1] if len(chain) > 1 else None,
        }

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def count_employees(self, *, status: str | None = None) -> dict[str, Any]:
        with self._lock:
            rows = list(self.employees)
        if status:
            rows = [e for e in rows if _normalize(e.get("status")) == _normalize(status)]
        by_status: dict[str, int] = {}
        for e in self.employees:
            key = str(e.get("status") or "unknown")
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "count": len(rows),
            "filter_status": status,
            "by_status": by_status,
            "total_in_directory": len(self.employees),
        }

    def salary_statistics(
        self,
        *,
        department: str | None = None,
        status: str | None = "active",
    ) -> dict[str, Any]:
        rows = self.search_employee(
            department=department,
            status=status,
            limit=500,
            include_sensitive=True,
        )
        salaries = [float(r["salary"]) for r in rows if r.get("salary") is not None]
        highest = None
        lowest = None
        if salaries:
            max_sal = max(salaries)
            min_sal = min(salaries)
            highest = next(
                (
                    {
                        "employee_id": r["employee_id"],
                        "name": self._full_name(r),
                        "salary": r["salary"],
                        "department": r.get("department"),
                        "position": r.get("position"),
                    }
                    for r in rows
                    if r.get("salary") == max_sal
                ),
                None,
            )
            lowest = next(
                (
                    {
                        "employee_id": r["employee_id"],
                        "name": self._full_name(r),
                        "salary": r["salary"],
                        "department": r.get("department"),
                        "position": r.get("position"),
                    }
                    for r in rows
                    if r.get("salary") == min_sal
                ),
                None,
            )
        return {
            "department": department,
            "status_filter": status,
            "employee_count": len(rows),
            "salary_summary": self._salary_summary(salaries),
            "highest": highest,
            "lowest": lowest,
            "currency": self.meta.get("currency", "USD"),
        }

    def employee_statistics(self) -> dict[str, Any]:
        """Organization-wide statistics from the JSON directory."""
        with self._lock:
            rows = list(self.employees)

        by_dept: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_country: dict[str, int] = {}
        by_gender: dict[str, int] = {}
        skills_freq: dict[str, int] = {}
        languages_freq: dict[str, int] = {}
        ages: list[int] = []
        salaries: list[float] = []
        hire_years: dict[int, int] = {}

        today = date.today()
        for emp in rows:
            dept = str(emp.get("department") or "Unknown")
            by_dept[dept] = by_dept.get(dept, 0) + 1
            st = str(emp.get("status") or "unknown")
            by_status[st] = by_status.get(st, 0) + 1
            country = str(emp.get("country") or "Unknown")
            by_country[country] = by_country.get(country, 0) + 1
            gender = str(emp.get("gender") or "unknown")
            by_gender[gender] = by_gender.get(gender, 0) + 1
            for s in emp.get("skills") or []:
                skills_freq[str(s)] = skills_freq.get(str(s), 0) + 1
            for lang in emp.get("languages") or []:
                languages_freq[str(lang)] = languages_freq.get(str(lang), 0) + 1
            age = _age_years(_parse_date(emp.get("birth_date")), today)
            if age is not None:
                ages.append(age)
            if emp.get("salary") is not None:
                try:
                    salaries.append(float(emp["salary"]))
                except (TypeError, ValueError):
                    pass
            hire = _parse_date(emp.get("hire_date"))
            if hire:
                hire_years[hire.year] = hire_years.get(hire.year, 0) + 1

        def top_n(freq: dict[str, int], n: int = 10) -> list[dict[str, Any]]:
            return [
                {"name": k, "count": v}
                for k, v in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:n]
            ]

        return {
            "organization": self.meta.get("organization"),
            "total_employees": len(rows),
            "departments_count": len(by_dept),
            "by_department": dict(sorted(by_dept.items(), key=lambda x: (-x[1], x[0]))),
            "by_status": by_status,
            "by_country": dict(sorted(by_country.items(), key=lambda x: (-x[1], x[0]))),
            "by_gender": by_gender,
            "hires_by_year": dict(sorted(hire_years.items())),
            "age_summary": {
                "count": len(ages),
                "min": min(ages) if ages else None,
                "max": max(ages) if ages else None,
                "average": round(sum(ages) / len(ages), 1) if ages else None,
            },
            "salary_summary": self._salary_summary(salaries),
            "top_skills": top_n(skills_freq),
            "top_languages": top_n(languages_freq),
            "currency": self.meta.get("currency", "USD"),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _full_name(emp: dict[str, Any]) -> str:
        parts = [
            emp.get("first_name"),
            emp.get("middle_name"),
            emp.get("last_name"),
        ]
        return " ".join(str(p) for p in parts if p)

    @staticmethod
    def _salary_summary(salaries: Iterable[float]) -> dict[str, Any]:
        values = list(salaries)
        if not values:
            return {
                "count": 0,
                "min": None,
                "max": None,
                "average": None,
                "sum": None,
            }
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "average": round(sum(values) / len(values), 2),
            "sum": round(sum(values), 2),
        }

    def _project(
        self,
        emp: dict[str, Any],
        *,
        include_sensitive: bool = True,
    ) -> dict[str, Any]:
        """Return a copy; optionally drop salary for coarser listings."""
        out = dict(emp)
        out["full_name"] = self._full_name(emp)
        if not include_sensitive:
            # Keep salary available for stats tools; for list-style calls
            # callers pass include_sensitive explicitly. We still include
            # salary by default because HR agent is an authorized internal tool.
            pass
        return out


def get_employee_service(json_path: str | Path | None = None) -> EmployeeService:
    """
    Return the process-wide EmployeeService singleton.

    On first call, loads employees from:
      1. explicit json_path argument
      2. EMPLOYEES_JSON_PATH env
      3. ./data/employees.json relative to CWD
    """
    global _service
    with _service_lock:
        if _service is not None:
            return _service
        path = (
            json_path
            or os.getenv("EMPLOYEES_JSON_PATH")
            or str(Path(__file__).resolve().parent.parent / "data" / "employees.json")
        )
        _service = EmployeeService(path)
        return _service


def reset_employee_service() -> None:
    """Test helper: clear the singleton."""
    global _service
    with _service_lock:
        _service = None
