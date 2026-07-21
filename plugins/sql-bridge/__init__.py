"""
Hermes plugin: sql-bridge

Registers toolset ``sql_bridge`` with a single tool ``sql_ask`` that delegates
to the LangGraph SQL agent (Variant 2 architecture).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.plugin.sql_bridge")


def _ensure_app_on_path() -> None:
    candidates = [
        Path(os.getenv("HR_APP_ROOT", os.getenv("APP_HOME", "/app"))),
        Path(__file__).resolve().parents[2],
    ]
    for root in candidates:
        if (root / "agents" / "sql_bridge_tool.py").is_file():
            s = str(root)
            if s not in sys.path:
                sys.path.insert(0, s)
            return


def register(ctx: Any) -> None:
    _ensure_app_on_path()
    try:
        from agents.sql_bridge_tool import (
            TOOL_NAME,
            TOOL_SCHEMA,
            TOOLSET_NAME,
            sql_ask_handler,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Cannot import sql_bridge_tool: %s", exc)
        return

    def _handle(params: dict, **kwargs: Any) -> str:
        del kwargs
        try:
            return sql_ask_handler(params or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("sql_ask failed")
            return json.dumps({"success": False, "error": str(exc)})

    try:
        ctx.register_tool(
            name=TOOL_NAME,
            toolset=TOOLSET_NAME,
            schema=TOOL_SCHEMA,
            handler=_handle,
            description=TOOL_SCHEMA.get("description", TOOL_NAME),
        )
        logger.info("Registered tool %s toolset=%s", TOOL_NAME, TOOLSET_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.error("ctx.register_tool failed: %s", exc)
