"""
FastAPI — Variant 2 Hermes host + SQL tool (+ additive document RAG).

  Open WebUI → Gateway → POST /v1/chat
       → Hermes host (context/memory)
            → tool sql_ask  → LangGraph SQL agent → PostgreSQL
            → tool docs_ask → RAG agent → Chroma (optional)

  Direct RAG: POST /v1/docs/chat
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
        description="Multi-turn session id (Hermes host memory)",
    )
    reset_session: bool = Field(
        default=False,
        description="Clear Hermes host session history",
    )


class ChatResponse(BaseModel):
    success: bool
    response: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retryable: Optional[bool] = None
    tools_called: Optional[list[dict[str, Any]]] = None
    tool_call_count: Optional[int] = None
    agents_used: Optional[list[str]] = None
    mode: Optional[str] = None
    backend: Optional[str] = None
    employee_count: Optional[int] = None


class DocsChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Document question")
    session_id: Optional[str] = Field(
        default=None,
        description="Optional client correlation id (RAG is stateless in v1)",
    )


class DocsChatResponse(BaseModel):
    success: bool
    response: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retryable: Optional[bool] = None
    sources: Optional[list[dict[str, Any]]] = None
    agents_used: Optional[list[str]] = None
    mode: Optional[str] = None
    backend: Optional[str] = None
    embed_provider: Optional[str] = None
    embed_model: Optional[str] = None
    embed_dim: Optional[int] = None


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
            "Variant 2: Hermes host agent + sql_ask tool → LangGraph SQL agent; "
            "optional docs_ask / POST /v1/docs/* document RAG (Chroma). "
            "Open WebUI gateway compatible (POST /v1/chat)."
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
        logger.info("Starting Hermes-host SQL service v%s", __version__)
        try:
            from agents.hermes_host import get_hermes_host

            host = get_hermes_host()
            logger.info("Hermes host readiness: %s", host.readiness())
        except Exception:
            logger.exception("Hermes host init failed — /ready may be 503")
        # RAG must never crash the process (Chroma can raise Rust PanicException).
        try:
            from agents.rag_agent import get_rag_agent, is_enabled as rag_enabled

            if rag_enabled():
                rag = get_rag_agent()
                logger.info("RAG readiness: %s", rag.readiness())
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logger.exception(
                "RAG init failed at startup (non-fatal): %s — /v1/docs/* may need reindex",
                exc,
            )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": os.getenv("APP_NAME", "ai-agents")}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        from agents.hermes_host import get_hermes_host

        host = get_hermes_host()
        if not host.ready:
            host.initialize()
        rd = host.readiness()
        if not rd.get("ready"):
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "host": rd},
            )
        return {"status": "ready", "host": rd}

    @app.get("/v1/self-improve")
    def self_improve_stats() -> dict[str, Any]:
        """Inspect the global self-improving recipe store (learned SQL patterns)."""
        from agents import self_improve

        return self_improve.stats()

    @app.get("/v1/docs/ready")
    def docs_ready() -> dict[str, Any]:
        from agents.rag_agent import get_rag_agent, is_enabled

        if not is_enabled():
            raise HTTPException(
                status_code=503,
                detail={"status": "disabled", "error": "RAG_ENABLED=false"},
            )
        rag = get_rag_agent()
        if not rag.ready:
            rag.initialize()
        rd = rag.readiness()
        if not rd.get("ready"):
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "rag": rd},
            )
        return {"status": "ready", "rag": rd}

    @app.get("/v1/docs/info")
    def docs_info() -> dict[str, Any]:
        from agents.rag_agent import get_rag_agent, is_enabled

        rag = get_rag_agent()
        return {
            "service": os.getenv("APP_NAME", "ai-agents"),
            "version": __version__,
            "design": "rag-chroma",
            "enabled": is_enabled(),
            "docs_chat_path": "/v1/docs/chat",
            "reindex_path": "/v1/docs/reindex",
            "rag": rag.readiness(),
        }

    @app.get("/v1/docs/files")
    def docs_files() -> dict[str, Any]:
        from agents.rag_agent import get_rag_agent

        return get_rag_agent().list_files()

    @app.post("/v1/docs/reindex")
    def docs_reindex(
        _: None = Depends(_check_bearer),
    ) -> dict[str, Any]:
        """Full rebuild of Chroma for the current RAG_EMBED_* identity."""
        from agents.rag_agent import get_rag_agent, is_enabled

        if not is_enabled():
            raise HTTPException(
                status_code=503,
                detail={"success": False, "error": "RAG_ENABLED=false"},
            )
        rag = get_rag_agent()
        logger.info("POST /v1/docs/reindex")
        result = rag.reindex(force=True)
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result)
        return result

    @app.post("/v1/docs/chat", response_model=DocsChatResponse)
    def docs_chat(
        body: DocsChatRequest,
        _: None = Depends(_check_bearer),
    ) -> DocsChatResponse:
        """Direct document RAG (bypasses Hermes host)."""
        from agents.rag_agent import get_rag_agent, is_enabled

        if not is_enabled():
            return DocsChatResponse(
                success=False,
                response=None,
                session_id=body.session_id,
                error="RAG disabled (RAG_ENABLED=false)",
                error_code="disabled",
                sources=[],
            )
        try:
            rag = get_rag_agent()
            logger.info(
                "POST /v1/docs/chat msg_len=%d preview=%r",
                len(body.message or ""),
                (body.message or "")[:80],
            )
            result = rag.chat(body.message)
        except Exception as exc:  # noqa: BLE001
            logger.error("docs chat failed: %s", exc, exc_info=True)
            return DocsChatResponse(
                success=False,
                response=None,
                session_id=body.session_id,
                error="Ichki server xatosi (RAG). Iltimos keyinroq urinib ko'ring.",
                error_code="internal",
                error_detail=str(exc)[:500],
                retryable=True,
                sources=[],
            )
        return DocsChatResponse(
            success=bool(result.get("success")),
            response=result.get("response"),
            session_id=body.session_id,
            error=result.get("error"),
            error_code=result.get("error_code"),
            error_detail=result.get("error_detail"),
            retryable=result.get("retryable"),
            sources=result.get("sources"),
            agents_used=result.get("agents_used"),
            mode=result.get("mode"),
            backend=result.get("backend"),
            embed_provider=result.get("embed_provider"),
            embed_model=result.get("embed_model"),
            embed_dim=result.get("embed_dim"),
        )

    @app.get("/v1/info")
    def info() -> dict[str, Any]:
        from agents.hermes_host import get_hermes_host

        host = get_hermes_host()
        rd = host.readiness()
        rag_rd: dict[str, Any] = {}
        try:
            from agents.rag_agent import get_rag_agent, is_enabled as rag_enabled

            rag_rd = {
                "enabled": rag_enabled(),
                "path": "/v1/docs/chat",
                "tool": "docs_ask",
                **(
                    {k: get_rag_agent().readiness().get(k) for k in (
                        "ready",
                        "identity",
                        "chunk_count",
                        "error",
                    )}
                    if rag_enabled()
                    else {}
                ),
            }
        except Exception as exc:  # noqa: BLE001
            rag_rd = {"enabled": False, "error": str(exc)}
        return {
            "service": os.getenv("APP_NAME", "ai-agents"),
            "version": __version__,
            "design": "hermes-host-sql-tool",
            "variant": 2,
            "architecture": rd.get("architecture"),
            "backend": rd.get("backend"),
            "gateway_compatible": True,
            "hermes_chat_path": "/v1/chat",
            "tool": "sql_ask",
            "inner_sql": rd.get("sql_agent"),
            "ready": host.ready,
            "model": rd.get("model"),
            "rag": rag_rd,
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(
        body: ChatRequest,
        _: None = Depends(_check_bearer),
    ) -> ChatResponse:
        """Gateway entry: Hermes host keeps context; SQL via sql_ask tool."""
        from agents.hermes_host import get_hermes_host

        try:
            host = get_hermes_host()
            logger.info(
                "POST /v1/chat session_id=%r reset=%s msg_len=%d msg_preview=%r",
                body.session_id,
                body.reset_session,
                len(body.message or ""),
                (body.message or "")[:80],
            )
            result = host.chat(
                body.message,
                session_id=body.session_id,
                reset_session=body.reset_session,
            )
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
            )
        return ChatResponse(
            success=bool(result.get("success")),
            response=result.get("response"),
            session_id=result.get("session_id") or body.session_id,
            error=result.get("error"),
            error_code=result.get("error_code"),
            error_detail=result.get("error_detail"),
            retryable=result.get("retryable"),
            tools_called=result.get("tools_called"),
            tool_call_count=result.get("tool_call_count"),
            agents_used=result.get("agents_used"),
            mode=result.get("mode"),
            backend=result.get("backend"),
        )

    return app


app = create_app()
