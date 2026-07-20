"""
FastAPI HTTP surface for the production SQL / HR AI Agent.

Endpoints:
  GET  /health          — liveness
  GET  /ready           — readiness (DB connected + agent init)
  GET  /v1/info         — service metadata
  POST /v1/chat         — SQL agent chat (Hermes AIAgent + SQL tools)
  POST /v1/tools/{name} — direct tool invocation (debug / automation)
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

logger = logging.getLogger("hr_agent")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User question for the SQL agent")
    session_id: Optional[str] = Field(
        default=None,
        description="Optional multi-turn session id",
    )
    reset_session: bool = Field(
        default=False,
        description="Clear prior history for session_id before this turn",
    )


class ChatResponse(BaseModel):
    success: bool
    response: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None
    db_ready: Optional[bool] = None
    # Dynamic tool trail (Langflow-style: variable length/order per question)
    tools_called: Optional[list[dict[str, Any]]] = None
    tool_call_count: Optional[int] = None


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
    """Optional bearer token gate (API_BEARER_TOKEN)."""
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
        title=os.getenv("APP_NAME", "hr-ai-agent"),
        version=__version__,
        description=(
            "Production SQL Agent powered by Nous Research Hermes Agent Framework. "
            "Knowledge source: PostgreSQL via read-only SQL tools."
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
        from agents.hr_agent import get_hr_agent

        logger.info("Starting SQL Agent API v%s", __version__)
        try:
            agent = get_hr_agent()
            logger.info("SQL Agent readiness: %s", agent.readiness())
        except Exception:
            logger.exception("Agent init on startup failed — /ready may return 503")

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness probe — process is up."""
        return {"status": "ok", "service": os.getenv("APP_NAME", "hr-ai-agent")}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        """Readiness probe — DB connected and agent initialized."""
        from agents.hr_agent import get_hr_agent
        from hr_tools.db_service import get_database_service

        agent = get_hr_agent()
        db = get_database_service().readiness()
        payload = {
            "status": "ready" if agent.ready and db.get("ready") else "not_ready",
            "agent": agent.readiness(),
            "database": db,
        }
        if payload["status"] != "ready":
            raise HTTPException(status_code=503, detail=payload)
        return payload

    @app.get("/v1/info")
    def info() -> dict[str, Any]:
        from agents.hr_agent import get_hr_agent
        from hr_tools.db_service import get_database_service

        agent = get_hr_agent()
        db = get_database_service().readiness()
        return {
            "service": os.getenv("APP_NAME", "hr-ai-agent"),
            "version": __version__,
            "framework": "hermes-agent (Nous Research)",
            "agent_type": "postgresql-sql-agent",
            "knowledge_source": "postgresql",
            "database": {
                "ready": db.get("ready"),
                "url_configured": db.get("database_url_configured"),
                "url_redacted": db.get("database_url_redacted"),
                "server_version": db.get("server_version"),
            },
            "toolsets": os.getenv("HR_ENABLED_TOOLSETS", "sql"),
            "model": agent.readiness().get("model"),
            "ready": agent.ready,
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(
        body: ChatRequest,
        _: None = Depends(_check_bearer),
    ) -> ChatResponse:
        from agents.hr_agent import get_hr_agent

        agent = get_hr_agent()
        result = agent.chat(
            body.message,
            session_id=body.session_id,
            reset_session=body.reset_session,
        )
        return ChatResponse(**{k: result.get(k) for k in ChatResponse.model_fields})

    @app.post("/v1/tools/{tool_name}")
    def invoke_tool(
        tool_name: str,
        body: ToolRequest,
        _: None = Depends(_check_bearer),
    ) -> dict[str, Any]:
        """
        Direct tool call for integration tests / automation.
        Returns the tool's JSON-decoded payload.
        """
        import json

        from hr_tools.sql_tool import get_tool_handlers

        handlers = get_tool_handlers()
        handler = handlers.get(tool_name)
        if handler is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown tool: {tool_name}. Available: {sorted(handlers)}",
            )
        raw = handler(body.arguments or {})
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    return app


# Module-level app for uvicorn "app.api:app"
app = create_app()
