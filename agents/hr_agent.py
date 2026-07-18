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


def _env_nonempty(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _detect_llm_provider(model: str) -> str:
    """
    Resolve which LLM backend to use.

    Hermes 0.18+ will not start an AIAgent with only an API key — it needs a
    resolvable provider (or an explicit base_url + api_key pair).
    """
    explicit = (
        _env_nonempty("HR_PROVIDER")
        or _env_nonempty("HERMES_PROVIDER")
        or ""
    ).lower()
    if explicit:
        return explicit

    model_l = (model or "").lower()
    has_openrouter = bool(_env_nonempty("OPENROUTER_API_KEY"))
    has_openai = bool(_env_nonempty("OPENAI_API_KEY"))
    has_anthropic = bool(_env_nonempty("ANTHROPIC_API_KEY"))

    # Model name hints take priority when the matching key is present.
    if "claude" in model_l or model_l.startswith("anthropic/"):
        if has_anthropic:
            return "anthropic"
        if has_openrouter:
            return "openrouter"
    if model_l.startswith("openai/") or model_l.startswith("gpt-") or model_l.startswith(
        "o1"
    ) or model_l.startswith("o3") or model_l.startswith("o4"):
        if has_openai:
            return "openai"
        if has_openrouter:
            return "openrouter"
    if model_l.startswith("openrouter/") or "/" in model_l:
        if has_openrouter:
            return "openrouter"

    if has_openrouter:
        return "openrouter"
    if has_openai:
        return "openai"
    if has_anthropic:
        return "anthropic"
    return ""


def _resolve_api_key(provider: str, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    override = _env_nonempty("HR_API_KEY")
    if override:
        return override
    if provider in {"openrouter"}:
        return _env_nonempty("OPENROUTER_API_KEY")
    if provider in {"openai", "openai-api", "custom"}:
        return _env_nonempty("OPENAI_API_KEY")
    if provider in {"anthropic"}:
        return _env_nonempty("ANTHROPIC_API_KEY")
    # Fallback order for unknown / empty provider
    return (
        _env_nonempty("OPENROUTER_API_KEY")
        or _env_nonempty("OPENAI_API_KEY")
        or _env_nonempty("ANTHROPIC_API_KEY")
    )


def _resolve_base_url(provider: str, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    configured = _env_nonempty("OPENAI_BASE_URL") or _env_nonempty("HR_BASE_URL")
    if configured:
        return configured
    # Hermes library mode needs an explicit base_url for direct OpenAI;
    # without it init fails with "No LLM provider configured".
    if provider in {"openai", "openai-api"}:
        return "https://api.openai.com/v1"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    return None


def _reasoning_config() -> dict[str, Any]:
    """
    Hermes defaults to reasoning.effort which OpenAI rejects for gpt-4o* models.

    Keep reasoning off unless HR_REASONING_ENABLED=true (for o-series / OR models).
    """
    if _env_bool("HR_REASONING_ENABLED", False):
        effort = _env_nonempty("HR_REASONING_EFFORT") or "medium"
        return {"enabled": True, "effort": effort}
    return {"enabled": False}


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

    model = model or _env_nonempty("HR_MODEL") or _env_nonempty("HERMES_MODEL") or ""
    provider = _detect_llm_provider(model)
    api_key = _resolve_api_key(provider, api_key)
    base_url = _resolve_base_url(provider, base_url)

    # Sensible default model when only OpenAI is configured
    if not model and provider in {"openai", "openai-api"}:
        model = "gpt-4o-mini"

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
        "reasoning_config": _reasoning_config(),
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    # Pass provider only when Hermes can resolve credentials for it; for OpenAI
    # the reliable library path is api_key + base_url (provider alone fails).
    if provider and provider not in {"openai", "openai-api", "custom"}:
        kwargs["provider"] = provider

    if not api_key:
        logger.warning(
            "No LLM API key resolved (provider=%r). Chat will fail until "
            "OPENAI_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY is set.",
            provider or "(none)",
        )

    logger.info(
        "Creating AIAgent model=%r provider=%r base_url=%r toolsets=%s max_iterations=%s",
        model or "(config default)",
        provider or "(inferred later)",
        base_url or "(default)",
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
            # Smoke-import Hermes (fail fast if missing / shadowed)
            try:
                import run_agent  # noqa: F401  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Hermes Agent Framework (run_agent) is not importable: "
                    f"{exc}. Ensure hermes-agent is installed and that a local "
                    "package named 'tools' is not shadowing Hermes's tools package."
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
