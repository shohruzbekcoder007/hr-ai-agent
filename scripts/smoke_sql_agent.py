#!/usr/bin/env python3
"""Smoke test LangChain SQLAgent."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agents.sql_agent import get_sql_agent  # noqa: E402


def main() -> int:
    try:
        agent = get_sql_agent()
        print("readiness:", json.dumps(agent.readiness(), ensure_ascii=False, indent=2))
        if not agent.ready:
            print("NOT READY:", agent.readiness().get("error"))
            return 1
        q = "rais o'rinbosa lavozimi bor shunda kimlar o'tiribdi?"
        print("question:", q)
        result = agent.chat(q)
        print("success:", result.get("success"))
        print("error_code:", result.get("error_code"))
        print("error:", result.get("error"))
        print("retryable:", result.get("retryable"))
        print("tool_call_count:", result.get("tool_call_count"))
        print("response:", result.get("response"))
        return 0 if result.get("success") else 2
    except Exception as exc:  # noqa: BLE001
        print("SMOKE UNCAUGHT:", type(exc).__name__, exc)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
