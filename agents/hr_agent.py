"""
HRAgent — specialized Hermes-based HR assistant.

Extends Hermes Agent Framework properly via:
  * AIAgent library interface (run_agent.AIAgent)
  * Custom system prompt (ephemeral_system_prompt)
  * HR toolset only (enabled_toolsets=["hr"])
  * Hermes plugin registration (plugins/hr-employee)

Does not rewrite Hermes. Does not use a vector DB. Knowledge = employees.json.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hr_agent")


def _read_text(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Required file not found: {p}")
    return p.read_text(encoding="utf-8")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_system_prompt() -> str:
    path = os.getenv(
        "SYSTEM_PROMPT_PATH",
        str(Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"),
    )
    return _read_text(path)


def _ensure_employee_data_loaded() -> dict[str, Any]:
    """Load employees.json via the HR tools service (startup requirement)."""
    from hr_tools.employee_service import get_employee_service

    path = os.getenv(
        "EMPLOYEES_JSON_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "employees.json"),
    )
    service = get_employee_service(path)
    readiness = service.readiness()
    if not readiness.get("ready"):
        raise RuntimeError(
            f"Employee data not ready: {readiness}. "
            f"Check EMPLOYEES_JSON_PATH={path}"
        )
    logger.info(
        "Employee knowledge base ready: %s employees from %s",
        readiness.get("employee_count"),
        readiness.get("json_path"),
    )
    return readiness


def _register_tools() -> list[str]:
    """Register HR tools with Hermes registry and force plugin discovery."""
    from hr_tools.employee_tool import register_hr_tools

    names = register_hr_tools()
    if names:
        logger.info("Hermes registry HR tools: %s", names)
    else:
        logger.info(
            "Hermes registry registration skipped or unavailable; "
            "plugin-based tools (toolset=hr) are still expected via HERMES_HOME plugins"
        )

    # Hermes discovers plugins as a side effect of model_tools import; call
    # discover_plugins() explicitly for library embedding (idempotent).
    try:
        import model_tools  # noqa: F401  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_tools import skipped: %s", exc)

    try:
        from hermes_cli.plugins import discover_plugins  # type: ignore[import-not-found]

        discover_plugins()
        logger.info("Hermes discover_plugins() completed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes discover_plugins() not available or failed: %s", exc)

    return names


def _build_ai_agent(
    *,
    system_prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Any:
    """
    Construct a Hermes AIAgent configured as the HR specialist.

    Raises ImportError if hermes-agent is not installed.
    """
    try:
        from run_agent import AIAgent  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "Hermes Agent Framework is required. Install with: "
            "pip install git+https://github.com/NousResearch/hermes-agent.git"
        ) from exc

    model = model or os.getenv("HR_MODEL") or os.getenv("HERMES_MODEL") or ""
    api_key = (
        api_key
        or os.getenv("HR_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
    )
    base_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("HR_BASE_URL")

    enabled_raw = os.getenv("HR_ENABLED_TOOLSETS", "hr")
    enabled_toolsets = [t.strip() for t in enabled_raw.split(",") if t.strip()]

    kwargs: dict[str, Any] = {
        "model": model,
        "quiet_mode": _env_bool("HR_QUIET_MODE", True),
        "max_iterations": _env_int("HR_MAX_ITERATIONS", 20),
        "enabled_toolsets": enabled_toolsets,
        "skip_memory": _env_bool("HR_SKIP_MEMORY", True),
        "skip_context_files": _env_bool("HR_SKIP_CONTEXT_FILES", True),
        "ephemeral_system_prompt": system_prompt,
        "platform": "hr-api",
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    logger.info(
        "Creating AIAgent model=%r toolsets=%s max_iterations=%s",
        model or "(config default)",
        enabled_toolsets,
        kwargs["max_iterations"],
    )
    return AIAgent(**kwargs)


class HRAgent:
    """
    Thread-safe façade around Hermes AIAgent for HR Q&A.

    One logical agent; create a fresh AIAgent per request when concurrent
    traffic is expected (Hermes agents are not thread-safe for shared use).
    """

    def __init__(
        self,
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        session_history_limit: int | None = None,
    ) -> None:
        self.system_prompt = system_prompt or _resolve_system_prompt()
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.session_history_limit = session_history_limit or _env_int(
            "HR_SESSION_HISTORY_LIMIT", 20
        )
        self._lock = threading.RLock()
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._employee_readiness: dict[str, Any] = {}
        self._initialized = False

    def initialize(self) -> dict[str, Any]:
        """Load JSON knowledge base and register tools. Idempotent."""
        with self._lock:
            if self._initialized:
                return {
                    "initialized": True,
                    "employee_readiness": self._employee_readiness,
                }
            logger.info("Initializing HR Agent...")
            self._employee_readiness = _ensure_employee_data_loaded()
            tool_names = _register_tools()
            # Smoke-import Hermes (fail fast if missing)
            try:
                import run_agent  # noqa: F401  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Hermes Agent Framework (run_agent) is not importable"
                ) from exc
            self._initialized = True
            logger.info(
                "HR Agent initialized (tools_registered=%s, employees=%s)",
                tool_names,
                self._employee_readiness.get("employee_count"),
            )
            return {
                "initialized": True,
                "employee_readiness": self._employee_readiness,
                "tools_registered": tool_names,
            }

    @property
    def ready(self) -> bool:
        return self._initialized and bool(
            self._employee_readiness.get("ready")
        )

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "initialized": self._initialized,
            "employee_readiness": self._employee_readiness,
            "model": self.model or os.getenv("HR_MODEL") or "",
        }

    def _new_agent(self) -> Any:
        return _build_ai_agent(
            system_prompt=self.system_prompt,
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def chat(
        self,
        message: str,
        *,
        session_id: str | None = None,
        reset_session: bool = False,
    ) -> dict[str, Any]:
        """
        Process one user message. Returns structured result.

        Parameters
        ----------
        message:
            User question (HR domain).
        session_id:
            Optional multi-turn session key. When set, conversation history
            is retained in-memory for this process.
        reset_session:
            Clear prior history for session_id before this turn.
        """
        if not self._initialized:
            self.initialize()

        message = (message or "").strip()
        if not message:
            return {
                "success": False,
                "error": "message must not be empty",
                "response": None,
                "session_id": session_id,
            }

        sid = session_id or str(uuid.uuid4())
        history: list[dict[str, Any]] | None = None

        with self._lock:
            if reset_session:
                self._sessions.pop(sid, None)
            if session_id is not None and sid in self._sessions:
                history = list(self._sessions[sid])

        agent = self._new_agent()
        logger.info("HR chat session_id=%s message_len=%d", sid, len(message))

        try:
            if history is not None:
                result = agent.run_conversation(
                    user_message=message,
                    conversation_history=history,
                    system_message=self.system_prompt,
                )
                final = result.get("final_response") if isinstance(result, dict) else str(result)
                messages = result.get("messages") if isinstance(result, dict) else None
            else:
                # Prefer run_conversation so system prompt is explicit
                result = agent.run_conversation(
                    user_message=message,
                    system_message=self.system_prompt,
                )
                if isinstance(result, dict):
                    final = result.get("final_response")
                    messages = result.get("messages")
                else:
                    final = str(result)
                    messages = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("HR agent conversation failed")
            return {
                "success": False,
                "error": str(exc),
                "response": None,
                "session_id": sid,
            }

        if messages and session_id is not None:
            # Keep a bounded history for multi-turn sessions
            trimmed = list(messages)
            # Approximate: keep last N message objects
            limit = max(4, self.session_history_limit * 2)
            if len(trimmed) > limit:
                trimmed = trimmed[-limit:]
            with self._lock:
                self._sessions[sid] = trimmed

        return {
            "success": True,
            "response": final,
            "session_id": sid,
            "employee_count": self._employee_readiness.get("employee_count"),
        }

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


# Process-wide singleton
_agent: Optional[HRAgent] = None
_agent_lock = threading.RLock()


def create_hr_agent(**kwargs: Any) -> HRAgent:
    """Create and initialize a new HRAgent instance."""
    agent = HRAgent(**kwargs)
    agent.initialize()
    return agent


def get_hr_agent() -> HRAgent:
    """Return the process-wide HRAgent singleton (created on first call)."""
    global _agent
    with _agent_lock:
        if _agent is None:
            _agent = create_hr_agent()
        elif not _agent.ready:
            _agent.initialize()
        return _agent
