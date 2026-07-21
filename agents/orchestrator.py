"""
Multi-agent orchestrator — single entry for Open WebUI / Gateway.

  Open WebUI → Gateway → POST /v1/chat → Orchestrator → [sql_agent, …]
                                                      ↓
                                              merged Hermes response

Modes (env AGENT_ORCHESTRATION_MODE):
  sql_only     — only SQL agent (default)
  sequential   — agent_1 then agent_2 (2nd sees 1st answer as context)
  parallel     — all enabled agents, then merge answers
  route        — pick one agent by simple keyword rules

Adding a new agent:
  1. Implement agents/<name>_agent.py with chat() / readiness() / ready
  2. Register it in _build_agents() below
  3. Enable via env (e.g. AGENT_EXTRA_ENABLED=true)
  Gateway URL stays the same: HERMES_GIS_BASE_URL=http://host.docker.internal:8080
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable

from agents.base import normalize_chat_result

logger = logging.getLogger("orchestrator")

_lock = threading.RLock()
_orchestrator: "AgentOrchestrator | None" = None


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


class AgentOrchestrator:
    """Combines one or more agents behind one Hermes-compatible chat API."""

    def __init__(self) -> None:
        self.mode = (_env("AGENT_ORCHESTRATION_MODE") or "sql_only").lower()
        self._agents: list[tuple[str, Any]] = []
        self._load_agents()

    def _load_agents(self) -> None:
        """Register enabled agents. Order matters for sequential mode."""
        agents: list[tuple[str, Any]] = []

        # --- always: SQL agent (Langflow SQLAgent) ---
        try:
            from agents.sql_agent import get_sql_agent

            sql = get_sql_agent()
            # attach name for logging if missing
            if not getattr(sql, "name", None):
                try:
                    sql.name = "sql"  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            agents.append(("sql", sql))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load sql_agent: %s", exc)

        # --- optional extra agent (enable when you implement it) ---
        # Set AGENT_EXTRA_ENABLED=true and provide agents/extra_agent.py
        if _env_bool("AGENT_EXTRA_ENABLED", False):
            try:
                from agents.extra_agent import get_extra_agent  # type: ignore

                extra = get_extra_agent()
                if not getattr(extra, "name", None):
                    try:
                        extra.name = "extra"  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
                agents.append(("extra", extra))
                logger.info("Extra agent registered")
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "AGENT_EXTRA_ENABLED=true but extra_agent failed to load: %s",
                    exc,
                )

        self._agents = agents
        logger.info(
            "Orchestrator mode=%s agents=%s",
            self.mode,
            [n for n, _ in self._agents],
        )

    def reload(self) -> None:
        with _lock:
            self.mode = (_env("AGENT_ORCHESTRATION_MODE") or "sql_only").lower()
            self._load_agents()

    @property
    def agent_names(self) -> list[str]:
        return [n for n, _ in self._agents]

    def readiness(self) -> dict[str, Any]:
        details: dict[str, Any] = {}
        any_ready = False
        for name, agent in self._agents:
            try:
                rd = agent.readiness() if hasattr(agent, "readiness") else {}
                ready = bool(getattr(agent, "ready", False) or rd.get("ready"))
                any_ready = any_ready or ready
                details[name] = {"ready": ready, **(rd if isinstance(rd, dict) else {})}
            except Exception as exc:  # noqa: BLE001
                details[name] = {"ready": False, "error": str(exc)}
        return {
            "ready": any_ready,
            "mode": self.mode,
            "agents": self.agent_names,
            "details": details,
            "entry": "POST /v1/chat (Hermes-compatible for Open WebUI gateway)",
        }

    @property
    def ready(self) -> bool:
        return bool(self.readiness().get("ready"))

    def chat(self, message: str, *, session_id: str | None = None) -> dict[str, Any]:
        del session_id  # reserved for multi-turn later
        message = (message or "").strip()
        if not message:
            return normalize_chat_result(
                {
                    "success": False,
                    "error": "message must not be empty",
                    "error_code": "validation",
                },
                agent_name="orchestrator",
            )

        if not self._agents:
            return normalize_chat_result(
                {
                    "success": False,
                    "error": "Hech qanday agent yuklanmagan",
                    "error_code": "no_agents",
                },
                agent_name="orchestrator",
            )

        mode = self.mode
        if mode == "sql_only" or len(self._agents) == 1:
            return self._run_single(self._agents[0][0], self._agents[0][1], message)

        if mode == "route":
            name, agent = self._route(message)
            return self._run_single(name, agent, message)

        if mode == "sequential":
            return self._run_sequential(message)

        if mode == "parallel":
            return self._run_parallel_merge(message)

        # unknown mode → first agent
        logger.warning("Unknown AGENT_ORCHESTRATION_MODE=%s; using first agent", mode)
        return self._run_single(self._agents[0][0], self._agents[0][1], message)

    def _run_single(self, name: str, agent: Any, message: str) -> dict[str, Any]:
        try:
            raw = agent.chat(message)
        except Exception as exc:  # noqa: BLE001
            logger.error("Agent %s failed: %s", name, exc, exc_info=True)
            return normalize_chat_result(
                {
                    "success": False,
                    "error": f"Agent '{name}' xatosi: {exc}",
                    "error_code": "agent_error",
                    "retryable": True,
                },
                agent_name=name,
            )
        out = normalize_chat_result(raw, agent_name=name)
        out["agents_used"] = [name]
        return out

    def _run_sequential(self, message: str) -> dict[str, Any]:
        """
        Agent1 → context → Agent2 → …
        Useful when extra agent enriches, then SQL answers with facts.
        """
        used: list[str] = []
        context_parts: list[str] = []
        last: dict[str, Any] = {}

        for name, agent in self._agents:
            if name == "sql" and context_parts:
                prompt = (
                    message
                    + "\n\n--- Context from previous agents ---\n"
                    + "\n\n".join(context_parts)
                )
            elif context_parts:
                prompt = (
                    message
                    + "\n\n--- Previous step ---\n"
                    + "\n\n".join(context_parts)
                )
            else:
                prompt = message

            try:
                raw = agent.chat(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.error("sequential agent %s failed: %s", name, exc)
                raw = {
                    "success": False,
                    "error": str(exc),
                    "error_code": "agent_error",
                }
            step = normalize_chat_result(raw, agent_name=name)
            used.append(name)
            last = step
            if step.get("success") and step.get("response"):
                context_parts.append(f"[{name}]: {step['response']}")
            elif step.get("error"):
                context_parts.append(f"[{name} ERROR]: {step['error']}")

        last["agents_used"] = used
        last["mode"] = "sequential"
        # Prefer last successful response; if last failed, try any success
        if not last.get("success"):
            # keep last error but attach partial context
            last["partial_context"] = context_parts
        return last

    def _run_parallel_merge(self, message: str) -> dict[str, Any]:
        """Run all agents; merge text answers."""
        used: list[str] = []
        chunks: list[str] = []
        tools: list[Any] = []
        any_ok = False
        errors: list[str] = []

        for name, agent in self._agents:
            try:
                raw = agent.chat(message)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                continue
            step = normalize_chat_result(raw, agent_name=name)
            used.append(name)
            if step.get("tools_called"):
                tools.extend(step["tools_called"])
            if step.get("success") and step.get("response"):
                any_ok = True
                chunks.append(f"**{name}**\n{step['response']}")
            elif step.get("error"):
                errors.append(f"{name}: {step['error']}")

        if not any_ok:
            return normalize_chat_result(
                {
                    "success": False,
                    "error": "; ".join(errors) or "Barcha agentlar muvaffaqiyatsiz",
                    "error_code": "all_failed",
                    "tools_called": tools,
                    "tool_call_count": len(tools),
                },
                agent_name="orchestrator",
            )

        merged = "\n\n---\n\n".join(chunks)
        out = normalize_chat_result(
            {
                "success": True,
                "response": merged,
                "tools_called": tools,
                "tool_call_count": len(tools),
            },
            agent_name="orchestrator",
        )
        out["agents_used"] = used
        out["mode"] = "parallel"
        if errors:
            out["partial_errors"] = errors
        return out

    def _route(self, message: str) -> tuple[str, Any]:
        """
        Simple keyword router. Extend rules when you add agents.

        Default: everything → sql
        """
        text = message.lower()
        # Example: if extra agent registered, route non-DB chit-chat there
        names = {n: a for n, a in self._agents}
        if "extra" in names:
            sql_hints = (
                r"\b(nechta|soni|xodim|ishchi|bo'?lim|lavozim|select|jadval|"
                r"department|employee|salary|markaziy|apparat|region)\b"
            )
            if not re.search(sql_hints, text, re.I):
                return "extra", names["extra"]
        # default sql
        if "sql" in names:
            return "sql", names["sql"]
        return self._agents[0]


def get_orchestrator() -> AgentOrchestrator:
    global _orchestrator
    with _lock:
        if _orchestrator is None:
            _orchestrator = AgentOrchestrator()
        return _orchestrator


def reset_orchestrator() -> None:
    """Test helper / hot-reload agents list."""
    global _orchestrator
    with _lock:
        _orchestrator = None
