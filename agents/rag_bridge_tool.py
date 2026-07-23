"""
Bridge tool: Hermes (or outer host) → document RAG agent.

Tool name: docs_ask
  question: natural language → Chroma retrieval + LLM answer (+ sources)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("rag_bridge")

TOOLSET_NAME = "docs_bridge"
TOOL_NAME = "docs_ask"

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Ask the internal document RAG agent about policies, rules, FAQs, PDF and "
        "Word files that have been indexed. Use for document/procedure/policy "
        "questions — not for live employee/org database counts or lists (use sql_ask "
        "for those). Returns an answer grounded in retrieved document excerpts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "Clear natural-language question about indexed documents "
                    "(policies, rules, PDF/Word content, FAQs)."
                ),
            },
        },
        "required": ["question"],
    },
}


def docs_ask_handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Hermes tool handler — never raises (errors become text for the model)."""
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

        from agents.rag_agent import get_rag_agent, is_enabled

        if not is_enabled():
            return json.dumps(
                {"success": False, "error": "RAG disabled (RAG_ENABLED=false)"},
                ensure_ascii=False,
            )

        agent = get_rag_agent()
        if not agent.ready:
            agent.initialize()
        result = agent.chat(question)

        if result.get("success") and result.get("response"):
            # Include short source list for the host model
            sources = result.get("sources") or []
            src_lines = []
            for s in sources[:5]:
                if not isinstance(s, dict):
                    continue
                page = s.get("page")
                page_s = f" p.{page}" if page is not None else ""
                src_lines.append(f"- {s.get('file')}{page_s}")
            text = str(result["response"])
            if src_lines:
                text = text + "\n\nSources:\n" + "\n".join(src_lines)
            return text

        return json.dumps(
            {
                "success": bool(result.get("success")),
                "answer": result.get("response"),
                "error": result.get("error"),
                "error_code": result.get("error_code"),
                "sources": result.get("sources"),
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("docs_ask_handler failed: %s", exc, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"docs_ask failed: {exc}"},
            ensure_ascii=False,
        )


def get_tool_handlers() -> dict[str, Callable[..., str]]:
    return {TOOL_NAME: docs_ask_handler}


def register_hermes_tools() -> list[str]:
    """Register docs_ask on Hermes tools.registry if available."""
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
            handler=docs_ask_handler,
            description=TOOL_SCHEMA["description"],
        )
        logger.info("Registered Hermes tool %s (toolset=%s)", TOOL_NAME, TOOLSET_NAME)
        return [TOOL_NAME]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes registry.register failed for docs_ask: %s", exc)
        return []


def as_langchain_tool():
    """LangChain StructuredTool wrapper (Hermes-lite outer agent)."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class DocsAskInput(BaseModel):
        question: str = Field(..., description="Natural-language document question")

    def _run(question: str) -> str:
        return docs_ask_handler({"question": question})

    return StructuredTool.from_function(
        name=TOOL_NAME,
        description=TOOL_SCHEMA["description"],
        func=_run,
        args_schema=DocsAskInput,
    )
