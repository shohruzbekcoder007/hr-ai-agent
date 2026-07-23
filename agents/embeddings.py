"""
Embedding factory for the RAG agent.

Only reads RAG_EMBED_* (and fallbacks OPENAI_*) env vars — no hard-coded model
in callers. Switch provider/model via env, then full reindex.

Providers:
  - remote | http  → embedding-service HTTP API (preferred in Docker)
  - openai         → langchain_openai (in-process)
  - local | huggingface → sentence-transformers / HF

Dimension isolation: every (provider, model, dim) maps to a unique index_key so
vectors from different models never share one Chroma directory.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("embeddings")

# Known defaults when RAG_EMBED_DIM is unset (in-process providers).
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
    remote_url: str = ""

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
            "remote_url": self.remote_url or None,
        }


def resolve_dim(provider: str, model: str, explicit: Optional[int]) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    key = (model or "").strip().lower()
    if key in _DEFAULT_DIMS:
        return _DEFAULT_DIMS[key]
    if key.endswith("/bge-m3") or key.endswith("bge-m3"):
        return 1024
    if provider in {"local", "huggingface", "hf"}:
        return 1024
    if "large" in key:
        return 3072
    if "small" in key or "ada" in key:
        return 1536
    return 1536


def _resolve_provider_name() -> str:
    """
    remote | http if RAG_EMBED_URL set and provider unset/remote;
    else openai | local | huggingface.
    """
    raw = (_env("RAG_EMBED_PROVIDER") or "").lower()
    url = _env("RAG_EMBED_URL")
    if raw in {"remote", "http"}:
        return "remote"
    if not raw and url:
        return "remote"
    if not raw:
        return "openai"
    if raw in {"hf", "huggingface"}:
        if _env("RAG_EMBED_BASE_URL"):
            return "huggingface"
        return "local"
    return raw


def get_embed_identity() -> EmbedIdentity:
    provider = _resolve_provider_name()

    if provider == "remote":
        return _remote_identity()

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
        remote_url="",
    )


def _remote_base_url() -> str:
    url = (_env("RAG_EMBED_URL") or "http://127.0.0.1:8090").rstrip("/")
    if not url:
        raise RuntimeError(
            "RAG_EMBED_PROVIDER=remote requires RAG_EMBED_URL "
            "(e.g. http://host.docker.internal:8090)"
        )
    return url


def _remote_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = (
        _env("RAG_EMBED_BEARER_TOKEN")
        or _env("EMBED_API_BEARER_TOKEN")
        or _env("API_BEARER_TOKEN")
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_remote_info(base_url: str) -> dict[str, Any]:
    import httpx

    timeout = float(_env("RAG_EMBED_TIMEOUT") or "60")
    with httpx.Client(timeout=timeout, headers=_remote_headers()) as client:
        # Prefer /v1/info; fall back to /ready
        for path in ("/v1/info", "/ready"):
            try:
                r = client.get(f"{base_url}{path}")
                if r.status_code >= 400:
                    continue
                data = r.json()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                logger.debug("remote info %s failed: %s", path, exc)
    raise RuntimeError(
        f"Cannot reach embedding-service at {base_url} "
        "(GET /v1/info or /ready failed). Is it running?"
    )


def _remote_identity() -> EmbedIdentity:
    """
    Identity comes from the remote service so index_key matches the
    vectors it actually produces (provider/model may differ from local env).
    """
    base_url = _remote_base_url()
    info = _fetch_remote_info(base_url)

    # Nested identity object from embedding-service readiness
    nested = info.get("identity") if isinstance(info.get("identity"), dict) else {}
    provider = str(
        nested.get("provider") or info.get("provider") or "remote"
    ).strip() or "remote"
    model = str(
        nested.get("model")
        or info.get("model")
        or _env("RAG_EMBED_MODEL")
        or "unknown"
    ).strip()
    dim_raw = nested.get("dim") if nested.get("dim") is not None else info.get("dim")
    try:
        dim = int(dim_raw) if dim_raw is not None else 0
    except (TypeError, ValueError):
        dim = 0
    if dim <= 0:
        dim = resolve_dim(provider, model, _env_int("RAG_EMBED_DIM", None))

    # Prefer remote index_key if present and consistent
    remote_key = nested.get("index_key") or info.get("index_key")
    ident = EmbedIdentity(
        provider=provider,
        model=model,
        dim=dim,
        api_key="",
        base_url=None,
        device=str(nested.get("device") or info.get("device") or "cpu"),
        remote_url=base_url,
    )
    if remote_key and str(remote_key) != ident.index_key:
        logger.warning(
            "Remote index_key=%s differs from local build=%s — using remote fields",
            remote_key,
            ident.index_key,
        )
    return ident


def get_embeddings(identity: EmbedIdentity | None = None) -> Any:
    """
    Build an embeddings client from env only.

    Providers:
      - remote | http: embedding-service (HTTP)
      - openai: langchain_openai.OpenAIEmbeddings
      - local / huggingface: sentence-transformers / HF
    """
    ident = identity or get_embed_identity()
    provider = ident.provider

    # If identity came from remote, client is always HTTP
    if ident.remote_url or _resolve_provider_name() == "remote":
        return _remote_embeddings(ident)

    if provider == "openai":
        return _openai_embeddings(ident)
    if provider in {"local", "huggingface"}:
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
        "Use: remote | http | openai | local | huggingface"
    )


class RemoteHTTPEmbeddings:
    """
    LangChain-compatible client for embedding-service.

    Methods: embed_query, embed_documents
    """

    def __init__(
        self,
        base_url: str,
        *,
        batch_size: int = 32,
        timeout: float = 120.0,
        expected_dim: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.expected_dim = expected_dim

    def embed_query(self, text: str) -> list[float]:
        import httpx

        payload = {"text": text, "input_type": "query"}
        with httpx.Client(timeout=self.timeout, headers=_remote_headers()) as client:
            r = client.post(f"{self.base_url}/v1/embed", json=payload)
            if r.status_code >= 400:
                raise RuntimeError(
                    f"remote embed failed HTTP {r.status_code}: {r.text[:500]}"
                )
            data = r.json()
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise RuntimeError("remote embed: missing embedding in response")
        return self._check_dim([float(x) for x in vec], data.get("dim"))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if not texts:
            return []
        out: list[list[float]] = []
        # Honor service EMBED_BATCH_MAX (default 64); use our batch_size
        step = self.batch_size
        with httpx.Client(timeout=self.timeout, headers=_remote_headers()) as client:
            for start in range(0, len(texts), step):
                chunk = texts[start : start + step]
                payload = {"texts": chunk, "input_type": "document"}
                r = client.post(f"{self.base_url}/v1/embed/batch", json=payload)
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"remote embed/batch failed HTTP {r.status_code}: "
                        f"{r.text[:500]}"
                    )
                data = r.json()
                vectors = data.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(chunk):
                    raise RuntimeError(
                        "remote embed/batch: embeddings count mismatch "
                        f"(got {len(vectors) if isinstance(vectors, list) else None}, "
                        f"expected {len(chunk)})"
                    )
                dim = data.get("dim")
                for v in vectors:
                    out.append(self._check_dim([float(x) for x in v], dim))
        return out

    def _check_dim(
        self, vec: list[float], reported_dim: Any = None
    ) -> list[float]:
        if self.expected_dim and len(vec) != int(self.expected_dim):
            raise RuntimeError(
                f"Remote embedding dim {len(vec)} != expected {self.expected_dim}"
            )
        if reported_dim is not None:
            try:
                rd = int(reported_dim)
                if rd and len(vec) != rd:
                    logger.warning(
                        "Remote reported dim=%s but vector length=%s",
                        rd,
                        len(vec),
                    )
            except (TypeError, ValueError):
                pass
        return vec


def _remote_embeddings(ident: EmbedIdentity) -> RemoteHTTPEmbeddings:
    base = ident.remote_url or _remote_base_url()
    batch = _env_int("RAG_EMBED_BATCH_SIZE", 32) or 32
    # Service default max is 64 — cap client batch
    batch = min(batch, _env_int("RAG_EMBED_REMOTE_BATCH_MAX", 64) or 64)
    timeout = float(_env("RAG_EMBED_TIMEOUT") or "120")
    logger.info(
        "Using remote embedding-service url=%s expected_dim=%s batch=%s",
        base,
        ident.dim,
        batch,
    )
    return RemoteHTTPEmbeddings(
        base,
        batch_size=batch,
        timeout=timeout,
        expected_dim=ident.dim,
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
