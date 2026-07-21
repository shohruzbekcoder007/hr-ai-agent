"""
Bridge tool: Hermes (or outer coordinator) → LangGraph SQL agent.

Tool name: sql_ask
  question: natural language → full SQL agent (list/schema/query) → answer text
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("sql_bridge")

TOOLSET_NAME = "sql_bridge"
TOOL_NAME = "sql_ask"

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Ask the internal LangGraph/LangChain PostgreSQL SQL agent a natural-language "
        "question about the workforce database. It inspects schema and runs read-only SQL. "
        "Use for any factual HR/org/employee/department/position/region question. "
        "Do not invent data — always call this tool for facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "Clear question for the SQL agent. Include filters from chat context "
                    "(names, departments, region/markaziy apparat, dates) when relevant."
                ),
            },
        },
        "required": ["question"],
    },
}


def sql_ask_handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """
    Hermes tool handler — returns a plain/JSON string for the host agent.
    Never raises (errors become text for the model).
    """
    del kwargs
    args = args or {}
    try:
        question = (
            args.get("question")
            or args.get("query")
            or args.get("message")
            or args.get("input")
            or ""
        )
        question = str(question).strip()
        if not question:
            return json.dumps(
                {"success": False, "error": "question is required"},
                ensure_ascii=False,
            )

        from agents.sql_agent import get_sql_agent

        agent = get_sql_agent()
        if not agent.ready:
            agent.initialize()
        if not agent.ready:
            return json.dumps(
                {
                    "success": False,
                    "error": agent.readiness().get("error") or "SQL agent not ready",
                },
                ensure_ascii=False,
            )

        result = agent.chat(question)
        # Compact payload for Hermes tool observation
        payload = {
            "success": bool(result.get("success")),
            "answer": result.get("response"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "tool_call_count": result.get("tool_call_count"),
        }
        # Prefer plain answer text when success — easier for host model
        if payload["success"] and payload.get("answer"):
            return str(payload["answer"])
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.error("sql_ask_handler failed: %s", exc, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"sql_ask failed: {exc}"},
            ensure_ascii=False,
        )


def get_tool_handlers() -> dict[str, Callable[..., str]]:
    return {TOOL_NAME: sql_ask_handler}


def register_hermes_tools() -> list[str]:
    """Register sql_ask on Hermes tools.registry if available."""
    try:
        from tools.registry import registry  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hermes registry not available: %s", exc)
        return []

    try:
        registry.register(
            name=TOOL_NAME,
            toolset=TOOLSET_NAME,
            schema=TOOL_SCHEMA,
            handler=sql_ask_handler,
            description=TOOL_SCHEMA["description"],
        )
        logger.info("Registered Hermes tool %s (toolset=%s)", TOOL_NAME, TOOLSET_NAME)
        return [TOOL_NAME]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes registry.register failed: %s", exc)
        return []


def as_langchain_tool():
    """LangChain StructuredTool wrapper (Hermes-lite outer agent)."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class SqlAskInput(BaseModel):
        question: str = Field(..., description="Natural-language DB question")

    def _run(question: str) -> str:
        return sql_ask_handler({"question": question})

    return StructuredTool.from_function(
        name=TOOL_NAME,
        description=TOOL_SCHEMA["description"],
        func=_run,
        args_schema=SqlAskInput,
    )
