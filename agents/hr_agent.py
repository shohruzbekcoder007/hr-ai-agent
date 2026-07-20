"""
HRAgent — Hermes-based PostgreSQL SQL Agent.

Extends Hermes Agent Framework properly via:
  * AIAgent library interface (run_agent.AIAgent)
  * Custom system prompt (ephemeral_system_prompt) — SQL expert + business dictionary
  * SQL toolset only (enabled_toolsets=["sql"])
  * Hermes plugin registration (plugins/hr-employee → toolset sql)

Does not rewrite Hermes. Knowledge source = PostgreSQL via SQL tools.
"""

from __future__ import annotations

import json
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


def _env_nonempty(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _resolve_system_prompt() -> str:
    path = os.getenv(
        "SYSTEM_PROMPT_PATH",
        str(Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"),
    )
    return _read_text(path)


def _ensure_database_ready() -> dict[str, Any]:
    """
    Probe PostgreSQL via DatabaseService.

    DATABASE_URL may be added later: if missing, readiness is not ready but
    tools remain registered so /v1/tools and startup can still work for wiring.
    Strict mode (HR_REQUIRE_DB=true, default) raises when URL is set but
    connection fails.
    """
    from hr_tools.db_service import get_database_service, resolve_database_url

    url = resolve_database_url()
    service = get_database_service(url)
    readiness = service.readiness()

    if not url:
        logger.warning(
            "DATABASE_URL is not set — SQL tools will fail until configured. "
            "Set DATABASE_URL=postgresql://user:pass@host:5432/dbname"
        )
        return readiness

    if readiness.get("ready"):
        logger.info(
            "PostgreSQL knowledge base ready: %s",
            readiness.get("database_url_redacted"),
        )
        return readiness

    # URL present but connection failed
    msg = (
        f"Database not ready: {readiness.get('error')}. "
        f"Check DATABASE_URL={readiness.get('database_url_redacted')}"
    )
    if _env_bool("HR_REQUIRE_DB", True):
        raise RuntimeError(msg)
    logger.error(msg)
    return readiness


def _register_tools() -> list[str]:
    """Register SQL tools with Hermes registry and force plugin discovery."""
    from hr_tools.sql_tool import register_sql_tools

    names = register_sql_tools()
    if names:
        logger.info("Hermes registry SQL tools: %s", names)
    else:
        logger.info(
            "Hermes registry registration skipped or unavailable; "
            "plugin-based tools (toolset=sql) are still expected via HERMES_HOME plugins"
        )

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


def _detect_llm_provider(model: str) -> str:
    """
    Hermes 0.18+ will not start an AIAgent with only an API key — it needs a
    resolvable provider. Prefer explicit HR_PROVIDER, then infer from model id
    and which API keys are present.
    """
    explicit = (_env_nonempty("HR_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit

    model_l = (model or "").strip().lower()
    has_openrouter = bool(_env_nonempty("OPENROUTER_API_KEY"))
    has_openai = bool(_env_nonempty("OPENAI_API_KEY"))
    has_anthropic = bool(_env_nonempty("ANTHROPIC_API_KEY"))

    if model_l.startswith("anthropic/") or model_l.startswith("claude"):
        if has_openrouter and not has_anthropic:
            return "openrouter"
        return "anthropic"
    if model_l.startswith("openai/") or model_l.startswith("gpt-") or model_l.startswith("o1") or model_l.startswith("o3"):
        if has_openrouter and not has_openai:
            return "openrouter"
        return "openai"
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
    if provider in {"openai", "openai-api"}:
        return "https://api.openai.com/v1"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    return None


def _reasoning_config() -> dict[str, Any]:
    if _env_bool("HR_REASONING_ENABLED", False):
        effort = _env_nonempty("HR_REASONING_EFFORT") or "medium"
        return {"enabled": True, "effort": effort}
    return {"enabled": False}


def _tool_display_name(name: str) -> str:
    try:
        from hr_tools.sql_tool import TOOL_DISPLAY_NAMES

        return TOOL_DISPLAY_NAMES.get(name, name)
    except Exception:  # noqa: BLE001
        return name


def _extract_tool_trail(
    messages: list[dict[str, Any]] | None,
    *,
    baseline_len: int = 0,
) -> list[dict[str, Any]]:
    """
    Extract ordered tool calls from Hermes conversation messages.

    Supports OpenAI-style tool_calls on assistant messages and role=tool rows.
    """
    if not messages:
        return []
    # Only messages from this turn when history was present
    slice_msgs = messages[baseline_len:] if baseline_len else messages
    trail: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for msg in slice_msgs:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "").lower()

        # Assistant tool_calls (OpenAI / Hermes)
        tool_calls = msg.get("tool_calls") or msg.get("function_call")
        if tool_calls and role in {"assistant", "model", ""}:
            if isinstance(tool_calls, dict):
                tool_calls = [tool_calls]
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                name = (
                    (fn or {}).get("name")
                    or tc.get("name")
                    or tc.get("tool")
                    or "unknown"
                )
                tid = str(tc.get("id") or tc.get("tool_call_id") or "")
                args_raw = (fn or {}).get("arguments") or tc.get("arguments") or {}
                if isinstance(args_raw, str):
                    try:
                        args_parsed: Any = json.loads(args_raw) if args_raw else {}
                    except Exception:  # noqa: BLE001
                        args_parsed = {"_raw": args_raw}
                else:
                    args_parsed = args_raw
                entry = {
                    "name": name,
                    "display_name": _tool_display_name(str(name)),
                    "arguments": args_parsed if isinstance(args_parsed, dict) else {},
                    "tool_call_id": tid or None,
                }
                if tid:
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                trail.append(entry)

        # Tool result messages
        if role == "tool":
            name = msg.get("name") or msg.get("tool_name")
            if name:
                # Attach result summary to last matching call if possible
                tid = str(msg.get("tool_call_id") or msg.get("id") or "")
                content = msg.get("content")
                preview = content
                if isinstance(content, str) and len(content) > 400:
                    preview = content[:400] + "…"
                matched = False
                if tid:
                    for item in reversed(trail):
                        if item.get("tool_call_id") == tid:
                            item["result_preview"] = preview
                            matched = True
                            break
                if not matched and name:
                    # Some stacks only emit tool role without prior tool_calls
                    trail.append(
                        {
                            "name": name,
                            "display_name": _tool_display_name(str(name)),
                            "arguments": {},
                            "result_preview": preview,
                        }
                    )

    return trail


def _build_ai_agent(
    *,
    system_prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Any:
    """
    Construct a Hermes AIAgent configured as the SQL specialist.

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

    if not model and provider in {"openai", "openai-api"}:
        model = "gpt-4o-mini"

    enabled_raw = os.getenv("HR_ENABLED_TOOLSETS", "sql")
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
    Thread-safe façade around Hermes AIAgent for SQL Q&A over PostgreSQL.

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
        self._db_readiness: dict[str, Any] = {}
        self._initialized = False
        # When DATABASE_URL is absent, still mark agent initialized so API
        # can start; /ready stays false until DB is configured and reachable.
        self._allow_init_without_db = not _env_bool("HR_REQUIRE_DB", True)

    def initialize(self) -> dict[str, Any]:
        """Probe DB (if configured), register SQL tools. Idempotent."""
        with self._lock:
            if self._initialized and self.ready:
                return {
                    "initialized": True,
                    "db_readiness": self._db_readiness,
                }
            logger.info("Initializing SQL Agent...")
            try:
                self._db_readiness = _ensure_database_ready()
            except RuntimeError:
                # Re-raise connection failures when URL is set and required
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Database probe failed")
                self._db_readiness = {
                    "ready": False,
                    "error": str(exc),
                    "database_url_configured": bool(
                        os.getenv("DATABASE_URL")
                        or os.getenv("HR_DATABASE_URL")
                        or os.getenv("POSTGRES_URL")
                    ),
                }
                if _env_bool("HR_REQUIRE_DB", True) and self._db_readiness.get(
                    "database_url_configured"
                ):
                    raise

            tool_names = _register_tools()
            try:
                import run_agent  # noqa: F401  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Hermes Agent Framework (run_agent) is not importable: "
                    f"{exc}. Ensure hermes-agent is installed and that a local "
                    "package named 'tools' is not shadowing Hermes's tools package."
                ) from exc

            # Initialized when tools are registered; ready when DB is up
            self._initialized = True
            logger.info(
                "SQL Agent initialized (tools_registered=%s, db_ready=%s)",
                tool_names,
                self._db_readiness.get("ready"),
            )
            return {
                "initialized": True,
                "db_readiness": self._db_readiness,
                "tools_registered": tool_names,
            }

    @property
    def ready(self) -> bool:
        return self._initialized and bool(self._db_readiness.get("ready"))

    def readiness(self) -> dict[str, Any]:
        # Refresh DB probe lightly when already initialized
        if self._initialized:
            try:
                from hr_tools.db_service import get_database_service

                self._db_readiness = get_database_service().readiness()
            except Exception as exc:  # noqa: BLE001
                self._db_readiness = {
                    **(self._db_readiness or {}),
                    "ready": False,
                    "error": str(exc),
                }
        return {
            "ready": self.ready,
            "initialized": self._initialized,
            "db_readiness": self._db_readiness,
            "model": self.model or os.getenv("HR_MODEL") or "",
            "toolsets": os.getenv("HR_ENABLED_TOOLSETS", "sql"),
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
        Process one user message via Hermes SQL tool-calling loop.
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

        if not self.ready:
            return {
                "success": False,
                "error": (
                    "Database is not ready. Set a working DATABASE_URL "
                    f"({(self._db_readiness or {}).get('error') or 'not configured'})"
                ),
                "response": None,
                "session_id": session_id,
            }

        sid = session_id or str(uuid.uuid4())
        history: list[dict[str, Any]] | None = None
        baseline_len = 0

        with self._lock:
            if reset_session:
                self._sessions.pop(sid, None)
            if session_id is not None and sid in self._sessions:
                history = list(self._sessions[sid])
                baseline_len = len(history)

        agent = self._new_agent()
        logger.info("SQL chat session_id=%s message_len=%d", sid, len(message))

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
            logger.exception("SQL agent conversation failed")
            return {
                "success": False,
                "error": str(exc),
                "response": None,
                "session_id": sid,
                "tools_called": [],
                "tool_call_count": 0,
            }

        tools_called = _extract_tool_trail(
            messages if isinstance(messages, list) else None,
            baseline_len=baseline_len,
        )
        if tools_called:
            logger.info(
                "SQL chat tools (%d): %s",
                len(tools_called),
                " → ".join(t.get("display_name") or t.get("name") for t in tools_called),
            )

        if messages and session_id is not None:
            trimmed = list(messages)
            limit = max(4, self.session_history_limit * 2)
            if len(trimmed) > limit:
                trimmed = trimmed[-limit:]
            with self._lock:
                self._sessions[sid] = trimmed

        return {
            "success": True,
            "response": final,
            "session_id": sid,
            "db_ready": self.ready,
            "tools_called": tools_called,
            "tool_call_count": len(tools_called),
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
            # Retry init / DB probe when previously not ready
            try:
                _agent.initialize()
            except Exception:
                logger.exception("Retry initialize failed")
        return _agent
