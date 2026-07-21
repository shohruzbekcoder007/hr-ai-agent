"""
Variant 2: Hermes host agent + SQL as a tool.

Architecture:

  User → Hermes host (conversation + memory)
            └─ tool sql_ask → LangGraph/LangChain SQL agent → PostgreSQL

If the Hermes package is unavailable, a Hermes-lite outer agent is used
(same design: host tool-calling loop + sql_ask tool + session history).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes_host")

_lock = threading.RLock()
_service: Optional["HermesHostService"] = None


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_coordinator_prompt() -> str:
    path = Path(
        _env("HERMES_SYSTEM_PROMPT_PATH")
        or str(
            Path(__file__).resolve().parent.parent
            / "prompts"
            / "hermes_coordinator.md"
        )
    )
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "You are a host agent. Use the sql_ask tool for any database facts. "
        "Never invent data."
    )


class HermesHostService:
    """
    Host agent with session memory. Tool sql_ask → LangGraph SQL agent.
    """

    name = "hermes_host"

    def __init__(self) -> None:
        self.model_name = (
            _env("HERMES_MODEL") or _env("LLM_MODEL") or _env("OPENAI_MODEL") or "gpt-4.1"
        )
        self.api_key = _env("OPENAI_API_KEY") or _env("LLM_API_KEY") or _env("HERMES_API_KEY")
        self.base_url = _env("OPENAI_BASE_URL") or _env("HERMES_BASE_URL") or None
        self.max_iterations = _env_int("HERMES_MAX_ITERATIONS", 12)
        self.session_limit = _env_int("HERMES_SESSION_HISTORY_LIMIT", 20)
        self.skip_memory = _env_bool("HERMES_SKIP_MEMORY", False)
        self.system_prompt = _load_coordinator_prompt()
        self._backend: str | None = None  # hermes | hermes_lite
        self._ready = False
        self._last_error: str | None = None
        self._sessions: dict[str, list[Any]] = {}
        # hermes_lite: langgraph graph; hermes: factory for AIAgent
        self._lite_graph: Any = None
        self._hermes_ok = False

    def initialize(self) -> dict[str, Any]:
        with _lock:
            if self._ready:
                return self.readiness()

            if not self.api_key:
                self._last_error = "OPENAI_API_KEY / HERMES_API_KEY not set"
                self._ready = False
                return self.readiness()

            # Warm SQL agent (tool target)
            try:
                from agents.sql_agent import get_sql_agent

                sql = get_sql_agent()
                if not sql.ready:
                    sql.initialize()
                if not sql.ready:
                    self._last_error = (
                        "Inner SQL agent not ready: "
                        f"{sql.readiness().get('error')}"
                    )
                    self._ready = False
                    return self.readiness()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"SQL agent init failed: {exc}"
                self._ready = False
                logger.error("%s", self._last_error)
                return self.readiness()

            # Register Hermes tools if possible
            try:
                from agents.sql_bridge_tool import register_hermes_tools

                register_hermes_tools()
            except Exception as exc:  # noqa: BLE001
                logger.debug("register_hermes_tools: %s", exc)

            try:
                from hermes_cli.plugins import discover_plugins  # type: ignore

                discover_plugins()
            except Exception as exc:  # noqa: BLE001
                logger.debug("discover_plugins: %s", exc)

            # Prefer real Hermes AIAgent
            if self._try_init_hermes():
                self._backend = "hermes"
                self._ready = True
                self._last_error = None
                logger.info("Hermes host ready (backend=hermes model=%s)", self.model_name)
                return self.readiness()

            # Hermes-lite: same architecture without hermes package
            if self._try_init_hermes_lite():
                self._backend = "hermes_lite"
                self._ready = True
                self._last_error = None
                logger.info(
                    "Hermes host ready (backend=hermes_lite model=%s) — "
                    "Hermes package missing or failed; using tool-calling host",
                    self.model_name,
                )
                return self.readiness()

            self._ready = False
            self._last_error = self._last_error or "Failed to init Hermes host"
            return self.readiness()

    def _try_init_hermes(self) -> bool:
        try:
            from run_agent import AIAgent  # type: ignore[import-not-found]

            # Smoke-construct (don't store shared AIAgent — not always thread-safe)
            kwargs = self._hermes_kwargs()
            _ = AIAgent(**kwargs)
            self._hermes_ok = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hermes AIAgent unavailable: %s", exc)
            self._hermes_ok = False
            self._last_error = f"Hermes unavailable: {exc}"
            return False

    def _reasoning_config(self) -> dict[str, Any]:
        """
        Hermes defaults to reasoning.effort which OpenAI gpt-4* / gpt-4.1 reject
        with HTTP 400. Keep off unless explicitly enabled (o-series etc.).
        """
        if _env_bool("HERMES_REASONING_ENABLED", False):
            effort = _env("HERMES_REASONING_EFFORT") or "medium"
            return {"enabled": True, "effort": effort}
        return {"enabled": False}

    def _hermes_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "quiet_mode": _env_bool("HERMES_QUIET_MODE", True),
            "max_iterations": self.max_iterations,
            "enabled_toolsets": [
                t.strip()
                for t in (_env("HERMES_ENABLED_TOOLSETS") or "sql_bridge").split(",")
                if t.strip()
            ],
            "skip_memory": self.skip_memory,
            "skip_context_files": _env_bool("HERMES_SKIP_CONTEXT_FILES", True),
            "ephemeral_system_prompt": self.system_prompt,
            "platform": "hermes-sql-host",
            "api_key": self.api_key,
            "reasoning_config": self._reasoning_config(),
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        # OpenAI path: don't force provider string
        return kwargs

    def _try_init_hermes_lite(self) -> bool:
        try:
            from langchain_openai import ChatOpenAI
            from langgraph.prebuilt import create_react_agent

            from agents.sql_bridge_tool import as_langchain_tool

            llm_kwargs: dict[str, Any] = {
                "model": self.model_name,
                "api_key": self.api_key,
                "temperature": 0,
            }
            if self.base_url:
                llm_kwargs["base_url"] = self.base_url
            llm = ChatOpenAI(**llm_kwargs)
            tools = [as_langchain_tool()]
            # Host agent: only sql_ask tool; SQL internals stay in LangGraph SQL agent
            self._lite_graph = create_react_agent(
                llm,
                tools,
                prompt=self.system_prompt,
            )
            return True
        except TypeError:
            # older create_react_agent without prompt=
            try:
                from langchain_openai import ChatOpenAI
                from langgraph.prebuilt import create_react_agent

                from agents.sql_bridge_tool import as_langchain_tool

                llm_kwargs = {
                    "model": self.model_name,
                    "api_key": self.api_key,
                    "temperature": 0,
                }
                if self.base_url:
                    llm_kwargs["base_url"] = self.base_url
                llm = ChatOpenAI(**llm_kwargs)
                self._lite_graph = create_react_agent(llm, [as_langchain_tool()])
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"hermes_lite init failed: {exc}"
                logger.error("%s", self._last_error)
                return False
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"hermes_lite init failed: {exc}"
            logger.error("%s", self._last_error)
            return False

    @property
    def ready(self) -> bool:
        return self._ready

    def readiness(self) -> dict[str, Any]:
        sql_rd: dict[str, Any] = {}
        try:
            from agents.sql_agent import get_sql_agent

            sql_rd = get_sql_agent().readiness()
        except Exception as exc:  # noqa: BLE001
            sql_rd = {"ready": False, "error": str(exc)}
        return {
            "ready": self._ready and bool(sql_rd.get("ready")),
            "backend": self._backend,
            "host": "hermes_variant_2",
            "architecture": (
                "Hermes host (memory/context) → tool sql_ask → "
                "LangGraph SQL agent → PostgreSQL"
            ),
            "model": self.model_name,
            "skip_memory": self.skip_memory,
            "session_count": len(self._sessions),
            "sql_agent": sql_rd,
            "error": self._last_error,
            "tool": "sql_ask",
            "toolset": "sql_bridge",
        }

    def clear_session(self, session_id: str) -> None:
        with _lock:
            self._sessions.pop(session_id, None)

    def chat(
        self,
        message: str,
        *,
        session_id: str | None = None,
        reset_session: bool = False,
    ) -> dict[str, Any]:
        """
        Host chat with optional multi-turn session.
        Never raises to callers.
        """
        try:
            message = (message or "").strip()
            if not message:
                return {
                    "success": False,
                    "response": None,
                    "error": "message must not be empty",
                    "error_code": "validation",
                    "session_id": session_id,
                }

            if not self._ready:
                self.initialize()
            if not self._ready:
                return {
                    "success": False,
                    "response": None,
                    "error": self._last_error or "Hermes host not ready",
                    "error_code": "not_ready",
                    "session_id": session_id,
                }

            client_sid = (session_id or "").strip() or None
            sid = client_sid or str(uuid.uuid4())
            if reset_session:
                self.clear_session(sid)

            prior_len = 0
            with _lock:
                prior_len = len(self._sessions.get(sid, []))
            logger.info(
                "host.chat client_session_id=%r effective_sid=%s prior_history=%d backend=%s",
                client_sid,
                sid,
                prior_len,
                self._backend,
            )

            if self._backend == "hermes":
                return self._chat_hermes(message, sid)
            return self._chat_hermes_lite(message, sid)
        except Exception as exc:  # noqa: BLE001
            logger.error("hermes_host.chat failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "response": None,
                "error": f"Host agent error: {exc}",
                "error_code": "agent_error",
                "retryable": True,
                "session_id": session_id,
            }

    def _chat_hermes(self, message: str, sid: str) -> dict[str, Any]:
        from run_agent import AIAgent  # type: ignore[import-not-found]

        history: list[Any] | None = None
        with _lock:
            if sid in self._sessions:
                history = list(self._sessions[sid])

        agent = AIAgent(**self._hermes_kwargs())
        try:
            if history:
                result = agent.run_conversation(
                    user_message=message,
                    conversation_history=history,
                    system_message=self.system_prompt,
                )
            else:
                result = agent.run_conversation(
                    user_message=message,
                    system_message=self.system_prompt,
                )
        except TypeError:
            # older signature
            result = agent.run_conversation(user_message=message)

        if isinstance(result, dict):
            final = result.get("final_response") or result.get("response")
            messages = result.get("messages")
        else:
            final = str(result)
            messages = None

        if messages is not None and not self.skip_memory:
            trimmed = list(messages)
            limit = max(4, self.session_limit * 2)
            if len(trimmed) > limit:
                trimmed = trimmed[-limit:]
            with _lock:
                self._sessions[sid] = trimmed

        return {
            "success": True,
            "response": final,
            "error": None,
            "session_id": sid,
            "backend": "hermes",
            "agents_used": ["hermes_host", "sql_ask→langgraph_sql"],
            "mode": "hermes_tool_sql",
        }

    def _chat_hermes_lite(self, message: str, sid: str) -> dict[str, Any]:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        with _lock:
            prior = list(self._sessions.get(sid, []))

        # Build message list: system + history + user
        messages: list[Any] = [SystemMessage(content=self.system_prompt)]
        messages.extend(prior)
        messages.append(HumanMessage(content=message))

        recursion = max(8, self.max_iterations * 2)
        try:
            result = self._lite_graph.invoke(
                {"messages": messages},
                config={"recursion_limit": recursion},
            )
        except TypeError:
            # graph without system in messages — prepend to user
            result = self._lite_graph.invoke(
                {
                    "messages": prior
                    + [HumanMessage(content=f"{self.system_prompt}\n\nUser: {message}")]
                },
                config={"recursion_limit": recursion},
            )

        out_msgs = result.get("messages") if isinstance(result, dict) else messages
        final = ""
        if out_msgs:
            last = out_msgs[-1]
            content = getattr(last, "content", None)
            if isinstance(content, list):
                final = " ".join(
                    b.get("text", str(b)) if isinstance(b, dict) else str(b)
                    for b in content
                )
            else:
                final = str(content if content is not None else last)

        # Persist history (human + ai turns only, bounded)
        if not self.skip_memory:
            new_hist = list(prior)
            new_hist.append(HumanMessage(content=message))
            new_hist.append(AIMessage(content=final or ""))
            limit = max(4, self.session_limit * 2)
            if len(new_hist) > limit:
                new_hist = new_hist[-limit:]
            with _lock:
                self._sessions[sid] = new_hist

        # Count sql_ask mentions roughly
        tool_hits = 0
        for m in out_msgs or []:
            tcs = getattr(m, "tool_calls", None) or []
            for tc in tcs:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                if name == "sql_ask":
                    tool_hits += 1

        return {
            "success": bool((final or "").strip()),
            "response": final or None,
            "error": None if (final or "").strip() else "Empty host response",
            "session_id": sid,
            "backend": "hermes_lite",
            "tool_call_count": tool_hits,
            "agents_used": ["hermes_host", "sql_ask→langgraph_sql"],
            "mode": "hermes_tool_sql",
        }


def get_hermes_host() -> HermesHostService:
    global _service
    with _lock:
        if _service is None:
            _service = HermesHostService()
            _service.initialize()
        elif not _service.ready:
            _service.initialize()
        return _service
