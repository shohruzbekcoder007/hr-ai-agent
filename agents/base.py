"""
Common contract for all agents that feed Open WebUI via the Hermes gateway.

Every agent must implement::

    def chat(self, message: str) -> dict
    # returns at least: success, response, error

    def readiness(self) -> dict
    @property ready -> bool
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentProtocol(Protocol):
    name: str

    @property
    def ready(self) -> bool: ...

    def readiness(self) -> dict[str, Any]: ...

    def chat(self, message: str) -> dict[str, Any]: ...


def normalize_chat_result(
    result: dict[str, Any] | None,
    *,
    agent_name: str = "",
) -> dict[str, Any]:
    """Ensure Hermes-compatible keys always exist."""
    r = dict(result or {})
    return {
        "success": bool(r.get("success")),
        "response": r.get("response"),
        "error": r.get("error"),
        "error_code": r.get("error_code"),
        "error_detail": r.get("error_detail"),
        "retryable": r.get("retryable"),
        "tools_called": r.get("tools_called") or [],
        "tool_call_count": r.get("tool_call_count") or 0,
        "agent": agent_name or r.get("agent") or r.get("backend"),
        "backend": r.get("backend"),
    }
