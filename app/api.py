"""
FastAPI surface for multi-agent service (Hermes-compatible for Open WebUI gateway).

  Open WebUI → Gateway → POST /v1/chat → Orchestrator → sql_agent [+ extra agents]
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import __version__

logger = logging.getLogger("app")


class ChatRequest(BaseModel):
    """Hermes-compatible chat body (gateway Open WebUI platform)."""

    message: str = Field(..., min_length=1, description="User question")
    session_id: Optional[str] = Field(
        default=None,
        description="Optional multi-turn session id (gateway may send)",
    )
    reset_session: bool = Field(
        default=False,
        description="Hermes-compatible flag; reserved",
    )


class ChatResponse(BaseModel):
    """
    Hermes-compatible response shape for gateway:

      { "success", "response", "session_id", "error", ... }
    """

    success: bool
    response: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None
    # Extra diagnostics (gateway may ignore unknown fields)
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retryable: Optional[bool] = None
    tools_called: Optional[list[dict[str, Any]]] = None
    tool_call_count: Optional[int] = None
    agents_used: Optional[list[str]] = None
    mode: Optional[str] = None
    # Old Hermes field (always null for SQL agent)
    employee_count: Optional[int] = None


class ToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _check_bearer(
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected = os.getenv("API_BEARER_TOKEN", "").strip()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("APP_NAME", "ai-agents"),
        version=__version__,
        description=(
            "LangChain SQLAgent service (Langflow flow: "
            "Chat Input → Prompt Template → SQLAgent → Chat Output)."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        logger.info("Starting AI Agents API v%s", __version__)
        try:
            from agents.orchestrator import get_orchestrator

            orch = get_orchestrator()
            logger.info("Orchestrator readiness: %s", orch.readiness())
        except Exception:
            logger.exception("Orchestrator init failed — /ready may be 503")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": os.getenv("APP_NAME", "ai-agents")}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        from agents.orchestrator import get_orchestrator

        orch = get_orchestrator()
        rd = orch.readiness()
        if not rd.get("ready"):
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "orchestrator": rd},
            )
        return {"status": "ready", "orchestrator": rd}

    @app.get("/v1/info")
    def info() -> dict[str, Any]:
        from agents.orchestrator import get_orchestrator

        orch = get_orchestrator()
        rd = orch.readiness()
        return {
            "service": os.getenv("APP_NAME", "ai-agents"),
            "version": __version__,
            "design": "multi-agent-orchestrator",
            "flow": (
                "Open WebUI → Gateway → POST /v1/chat → "
                "Orchestrator → sql_agent [+ extra agents]"
            ),
            "gateway_compatible": True,
            "hermes_chat_path": "/v1/chat",
            "orchestration": {
                "mode": rd.get("mode"),
                "agents": rd.get("agents"),
            },
            "details": rd.get("details"),
            "ready": orch.ready,
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(
        body: ChatRequest,
        _: None = Depends(_check_bearer),
    ) -> ChatResponse:
        """Hermes-compatible entry used by Open WebUI platform gateway."""
        from agents.orchestrator import get_orchestrator

        try:
            orch = get_orchestrator()
            result = orch.chat(body.message, session_id=body.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("chat endpoint failed: %s", exc, exc_info=True)
            return ChatResponse(
                success=False,
                response=None,
                session_id=body.session_id,
                error="Ichki server xatosi. Iltimos keyinroq urinib ko'ring.",
                error_code="internal",
                error_detail=str(exc)[:500],
                retryable=True,
                tools_called=[],
                tool_call_count=0,
            )
        return ChatResponse(
            success=bool(result.get("success")),
            response=result.get("response"),
            session_id=body.session_id,
            error=result.get("error"),
            error_code=result.get("error_code"),
            error_detail=result.get("error_detail"),
            retryable=result.get("retryable"),
            tools_called=result.get("tools_called"),
            tool_call_count=result.get("tool_call_count"),
            agents_used=result.get("agents_used"),
            mode=result.get("mode"),
        )

    return app


app = create_app()
