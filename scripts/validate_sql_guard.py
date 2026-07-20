#!/usr/bin/env python3
"""Unit checks for read-only SQL validation (no database required)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hr_tools.db_service import validate_readonly_sql  # noqa: E402


def expect_ok(sql: str) -> None:
    out = validate_readonly_sql(sql)
    assert out, sql
    print("OK allow:", sql[:60].replace("\n", " "))


def expect_fail(sql: str) -> None:
    try:
        validate_readonly_sql(sql)
    except ValueError as exc:
        print("OK deny:", sql[:50].replace("\n", " "), "->", exc)
        return
    raise AssertionError(f"expected rejection: {sql}")


def main() -> int:
    expect_ok("SELECT id, name FROM departments LIMIT 10")
    expect_ok("WITH x AS (SELECT 1 AS n) SELECT * FROM x")
    expect_ok("EXPLAIN SELECT 1")
    expect_ok("SELECT count(*) FROM employees;")

    expect_fail("INSERT INTO employees (first_name) VALUES ('x')")
    expect_fail("UPDATE employees SET first_name='x'")
    expect_fail("DELETE FROM employees")
    expect_fail("DROP TABLE employees")
    expect_fail("SELECT 1; SELECT 2")
    expect_fail("EXPLAIN ANALYZE SELECT 1")
    expect_fail("")

    print("All SQL guard checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
