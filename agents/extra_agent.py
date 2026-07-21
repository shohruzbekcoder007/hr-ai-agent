"""
Optional second agent template.

Enable with:
  AGENT_EXTRA_ENABLED=true
  AGENT_ORCHESTRATION_MODE=sequential|parallel|route

Replace chat() logic with your own (LLM, tools, another DB, etc.).
Until customized, this is a lightweight OpenAI chat helper (no SQL).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("extra_agent")

_lock = threading.RLock()
_service: Optional["ExtraAgentService"] = None


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


class ExtraAgentService:
    """Placeholder agent — customize for your domain."""

    name = "extra"

    def __init__(self) -> None:
        self.model_name = _env("EXTRA_LLM_MODEL") or _env("LLM_MODEL") or "gpt-4.1"
        self.api_key = _env("OPENAI_API_KEY") or _env("LLM_API_KEY")
        self.base_url = _env("OPENAI_BASE_URL") or None
        self.system_prompt = self._load_prompt()
        self._ready = bool(self.api_key)
        self._last_error: str | None = None if self._ready else "OPENAI_API_KEY missing"

    def _load_prompt(self) -> str:
        path = Path(
            _env("EXTRA_SYSTEM_PROMPT_PATH")
            or str(
                Path(__file__).resolve().parent.parent
                / "prompts"
                / "extra_agent_system.md"
            )
        )
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return (
            "You are a helpful specialist agent working together with a SQL agent. "
            "Answer clearly. If the question needs database facts, say so briefly."
        )

    @property
    def ready(self) -> bool:
        return self._ready

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": self._ready,
            "model": self.model_name,
            "error": self._last_error,
            "agent_type": "extra-llm",
        }

    def initialize(self) -> dict[str, Any]:
        self._ready = bool(self.api_key)
        self._last_error = None if self._ready else "OPENAI_API_KEY missing"
        return self.readiness()

    def chat(self, message: str) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            return {"success": False, "response": None, "error": "empty message"}
        if not self.api_key:
            return {
                "success": False,
                "response": None,
                "error": "OPENAI_API_KEY not set for extra agent",
                "error_code": "config",
            }
        try:
            from langchain_openai import ChatOpenAI
            from langchain_core.messages import HumanMessage, SystemMessage

            kwargs: dict[str, Any] = {
                "model": self.model_name,
                "api_key": self.api_key,
                "temperature": 0.2,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            llm = ChatOpenAI(**kwargs)
            resp = llm.invoke(
                [
                    SystemMessage(content=self.system_prompt),
                    HumanMessage(content=message),
                ]
            )
            text = getattr(resp, "content", None) or str(resp)
            if isinstance(text, list):
                text = " ".join(
                    b.get("text", str(b)) if isinstance(b, dict) else str(b)
                    for b in text
                )
            return {
                "success": True,
                "response": str(text).strip(),
                "error": None,
                "agent": self.name,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("extra agent chat failed: %s", exc)
            return {
                "success": False,
                "response": None,
                "error": str(exc),
                "error_code": "agent_error",
                "retryable": True,
            }


def get_extra_agent() -> ExtraAgentService:
    global _service
    with _lock:
        if _service is None:
            _service = ExtraAgentService()
            _service.initialize()
        return _service
