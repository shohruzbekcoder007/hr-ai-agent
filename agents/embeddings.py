"""
Embedding factory for the RAG agent.

Only reads RAG_EMBED_* (and fallbacks OPENAI_*) env vars — no hard-coded model
choice in callers. Switch provider/model via env, then full reindex.

Dimension isolation: every (provider, model, dim) maps to a unique index_key so
OpenAI 1536/3072 vectors never mix with BAAI/bge-m3 1024 (etc.).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("embeddings")

# Known defaults when RAG_EMBED_DIM is unset.
_DEFAULT_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "baai/bge-m3": 1024,
    "bge-m3": 1024,
}


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _safe_token(value: str) -> str:
    s = (value or "").strip().replace("/", "_").replace("\\", "_").replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", s)
    return s[:120] or "model"


@runtime_checkable
class EmbeddingsProtocol(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class EmbedIdentity:
    """Resolved embedding config used for Chroma path + manifest checks."""

    provider: str
    model: str
    dim: int
    api_key: str
    base_url: Optional[str]
    device: str

    @property
    def index_key(self) -> str:
        return f"{_safe_token(self.provider)}__{_safe_token(self.model)}__d{self.dim}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dim": self.dim,
            "index_key": self.index_key,
            "device": self.device,
            "base_url_set": bool(self.base_url),
            "api_key_set": bool(self.api_key),
        }


def resolve_dim(provider: str, model: str, explicit: Optional[int]) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    key = (model or "").strip().lower()
    if key in _DEFAULT_DIMS:
        return _DEFAULT_DIMS[key]
    # HuggingFace-style org/name
    if key.endswith("/bge-m3") or key.endswith("bge-m3"):
        return 1024
    if provider in {"local", "huggingface", "hf"}:
        return 1024
    # OpenAI family fallback
    if "large" in key:
        return 3072
    if "small" in key or "ada" in key:
        return 1536
    return 1536


def get_embed_identity() -> EmbedIdentity:
    provider = (_env("RAG_EMBED_PROVIDER") or "openai").lower()
    if provider in {"hf", "huggingface"}:
        # Alias: HF inference may use base_url; local weights use "local"
        if _env("RAG_EMBED_BASE_URL"):
            provider = "huggingface"
        else:
            provider = "local"

    model = _env("RAG_EMBED_MODEL") or "text-embedding-3-small"
    explicit_dim = _env_int("RAG_EMBED_DIM", None)
    dim = resolve_dim(provider, model, explicit_dim)

    api_key = (
        _env("RAG_EMBED_API_KEY")
        or _env("OPENAI_API_KEY")
        or _env("LLM_API_KEY")
        or _env("HERMES_API_KEY")
    )
    base_url = _env("RAG_EMBED_BASE_URL") or _env("OPENAI_BASE_URL") or None
    if base_url == "":
        base_url = None
    device = _env("RAG_EMBED_DEVICE") or "cpu"

    return EmbedIdentity(
        provider=provider,
        model=model,
        dim=dim,
        api_key=api_key,
        base_url=base_url,
        device=device,
    )


def get_embeddings(identity: EmbedIdentity | None = None) -> Any:
    """
    Build an embeddings client from env only.

    Providers:
      - openai (default): langchain_openai.OpenAIEmbeddings
      - local: sentence-transformers / HuggingFaceEmbeddings (e.g. BAAI/bge-m3)
      - huggingface: OpenAI-compatible or HF endpoint via base_url when set;
        otherwise same as local
    """
    ident = identity or get_embed_identity()
    provider = ident.provider

    if provider == "openai":
        return _openai_embeddings(ident)
    if provider in {"local", "huggingface"}:
        # If base_url set and provider huggingface → try OpenAI-compatible first
        if provider == "huggingface" and ident.base_url:
            try:
                return _openai_embeddings(ident)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "huggingface base_url OpenAI-compatible client failed (%s); "
                    "falling back to local weights",
                    exc,
                )
        return _local_embeddings(ident)

    raise ValueError(
        f"Unknown RAG_EMBED_PROVIDER={provider!r}. "
        "Use: openai | local | huggingface"
    )


def _openai_embeddings(ident: EmbedIdentity) -> Any:
    from langchain_openai import OpenAIEmbeddings

    if not ident.api_key:
        raise RuntimeError(
            "OPENAI_API_KEY / RAG_EMBED_API_KEY required for RAG_EMBED_PROVIDER=openai"
        )

    kwargs: dict[str, Any] = {
        "model": ident.model,
        "api_key": ident.api_key,
    }
    if ident.base_url:
        kwargs["base_url"] = ident.base_url
    # OpenAI embedding-3 supports dimensions= for reduced size; include when set
    # via RAG_EMBED_DIM (and model is not ada-002 which ignores it).
    explicit = _env_int("RAG_EMBED_DIM", None)
    if explicit and explicit > 0 and "ada-002" not in (ident.model or "").lower():
        kwargs["dimensions"] = explicit

    return OpenAIEmbeddings(**kwargs)


def _local_embeddings(ident: EmbedIdentity) -> Any:
    """
    BAAI/bge-m3 and other sentence-transformers models.

    Requires optional deps: sentence-transformers (and torch).
    """
    model_name = ident.model or "BAAI/bge-m3"
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        except ImportError as exc:
            raise RuntimeError(
                "Local embeddings require sentence-transformers / langchain-huggingface. "
                "Install: pip install -r requirements-rag-local.txt "
                f"(model={model_name})"
            ) from exc

    try:
        return HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": ident.device or "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Failed to load local embedding model {model_name!r}: {exc}. "
            "Install: pip install -r requirements-rag-local.txt"
        ) from exc
