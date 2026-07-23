"""
Document RAG agent: PDF / Word / MD / TXT → embed → Chroma → answer + sources.

Independent of Hermes/SQL. Switch embedding model via env + full reindex.
Index path is isolated per (provider, model, dim).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("rag_agent")

_lock = threading.RLock()
_service: Optional["RAGAgentService"] = None

_SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".markdown"}


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


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


def is_enabled() -> bool:
    return _env_bool("RAG_ENABLED", True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _docs_dir() -> Path:
    return Path(_env("RAG_DOCS_DIR") or str(_repo_root() / "data" / "docs"))


def _chroma_root() -> Path:
    return Path(_env("RAG_CHROMA_ROOT") or str(_repo_root() / "data" / "rag" / "chroma"))


def _prompt_path() -> Path:
    return Path(
        _env("RAG_SYSTEM_PROMPT_PATH")
        or str(_repo_root() / "prompts" / "rag_agent_system.md")
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chunk_id(source: str, page: Any, index: int, text: str) -> str:
    raw = f"{source}|{page}|{index}|{text[:200]}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:40]


def load_system_prompt() -> str:
    path = _prompt_path()
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "Answer only from the provided document context. "
        "If missing, say the answer was not found in the documents."
    )


# ---------------------------------------------------------------------------
# document loaders
# ---------------------------------------------------------------------------


def _load_pdf(path: Path) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    out: list[dict[str, Any]] = []
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        text = text.strip()
        if not text:
            continue
        out.append(
            {
                "text": text,
                "source": path.name,
                "path": str(path),
                "page": i + 1,
                "file_type": "pdf",
            }
        )
    return out


def _load_docx(path: Path) -> list[dict[str, Any]]:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    # tables
    for table in doc.tables:
        for row in table.rows:
            cells = [((c.text or "").strip()) for c in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                parts.append(line)
    text = "\n".join(parts).strip()
    if not text:
        return []
    return [
        {
            "text": text,
            "source": path.name,
            "path": str(path),
            "page": None,
            "file_type": "docx",
        }
    ]


def _load_text(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = text.strip()
    if not text:
        return []
    suffix = path.suffix.lower().lstrip(".") or "txt"
    return [
        {
            "text": text,
            "source": path.name,
            "path": str(path),
            "page": None,
            "file_type": suffix,
        }
    ]


def load_file(path: Path) -> list[dict[str, Any]]:
    suf = path.suffix.lower()
    if suf == ".pdf":
        return _load_pdf(path)
    if suf == ".docx":
        return _load_docx(path)
    if suf in {".md", ".txt", ".markdown"}:
        return _load_text(path)
    return []


def discover_files(docs_dir: Path) -> list[Path]:
    if not docs_dir.is_dir():
        return []
    files: list[Path] = []
    for p in sorted(docs_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in _SUPPORTED_SUFFIXES:
            files.append(p)
    return files


def _split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        return [c.strip() for c in splitter.split_text(text) if c.strip()]
    except Exception:  # noqa: BLE001
        # Fallback: simple window
        if len(text) <= chunk_size:
            return [text]
        chunks: list[str] = []
        step = max(1, chunk_size - overlap)
        for i in range(0, len(text), step):
            part = text[i : i + chunk_size].strip()
            if part:
                chunks.append(part)
            if i + chunk_size >= len(text):
                break
        return chunks


class RAGAgentService:
    name = "rag_agent"

    def __init__(self) -> None:
        self._ready = False
        self._last_error: str | None = None
        self._identity: Any = None
        self._embeddings: Any = None
        self._collection: Any = None
        self._client: Any = None
        self._manifest: dict[str, Any] = {}
        self._system_prompt = load_system_prompt()
        self.top_k = _env_int("RAG_TOP_K", 5)
        self.chunk_size = _env_int("RAG_CHUNK_SIZE", 800)
        self.chunk_overlap = _env_int("RAG_CHUNK_OVERLAP", 120)
        self.batch_size = _env_int("RAG_EMBED_BATCH_SIZE", 32)

    @property
    def ready(self) -> bool:
        return self._ready and is_enabled()

    def _index_dir(self) -> Path:
        from agents.embeddings import get_embed_identity

        ident = self._identity or get_embed_identity()
        return _chroma_root() / ident.index_key

    def _manifest_path(self) -> Path:
        return self._index_dir() / "manifest.json"

    def _read_manifest(self) -> dict[str, Any]:
        path = self._manifest_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_manifest(self, data: dict[str, Any]) -> None:
        path = self._manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        self._manifest = data

    def _identity_matches_manifest(self) -> bool:
        from agents.embeddings import get_embed_identity

        ident = self._identity or get_embed_identity()
        m = self._manifest or self._read_manifest()
        if not m:
            return False
        return (
            str(m.get("provider")) == ident.provider
            and str(m.get("model")) == ident.model
            and int(m.get("dim") or 0) == int(ident.dim)
        )

    def initialize(self) -> dict[str, Any]:
        with _lock:
            if not is_enabled():
                self._ready = False
                self._last_error = "RAG_ENABLED=false"
                return self.readiness()

            try:
                from agents.embeddings import get_embed_identity, get_embeddings

                self._identity = get_embed_identity()
                self._embeddings = get_embeddings(self._identity)
                self._system_prompt = load_system_prompt()
                self.top_k = _env_int("RAG_TOP_K", 5)
                self.chunk_size = _env_int("RAG_CHUNK_SIZE", 800)
                self.chunk_overlap = _env_int("RAG_CHUNK_OVERLAP", 120)
                self.batch_size = _env_int("RAG_EMBED_BATCH_SIZE", 32)

                index_dir = self._index_dir()
                index_dir.mkdir(parents=True, exist_ok=True)
                # Chroma open can raise Rust PanicException (not subclass of Exception)
                # on corrupt / cross-OS volume indexes — never kill the process.
                self._open_chroma(index_dir)
                self._manifest = self._read_manifest()

                if self._manifest and not self._identity_matches_manifest():
                    self._last_error = (
                        "Index embed identity mismatch "
                        f"(index={self._manifest.get('provider')}/"
                        f"{self._manifest.get('model')}/d{self._manifest.get('dim')}; "
                        f"env={self._identity.provider}/{self._identity.model}/"
                        f"d{self._identity.dim}). Call POST /v1/docs/reindex."
                    )
                    if _env_bool("RAG_AUTO_REINDEX_ON_MISMATCH", False):
                        logger.warning("%s — auto reindex", self._last_error)
                        return self.reindex(force=True)
                    # Still "ready" to accept reindex; search will refuse
                    self._ready = True
                    return self.readiness()

                self._ready = True
                self._last_error = None
                logger.info(
                    "RAG agent ready provider=%s model=%s dim=%s index=%s chunks=%s",
                    self._identity.provider,
                    self._identity.model,
                    self._identity.dim,
                    index_dir,
                    self._chunk_count(),
                )
                return self.readiness()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self._ready = False
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._client = None
                self._collection = None
                logger.error("RAG initialize failed: %s", self._last_error, exc_info=True)
                return self.readiness()

    def _close_chroma(self) -> None:
        """Release client handles and Chroma process-wide cache before rmtree."""
        self._collection = None
        self._client = None
        try:
            from chromadb.api.shared_system_client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception as exc:  # noqa: BLE001
            logger.debug("chroma clear_system_cache: %s", exc)

    def _open_chroma(self, index_dir: Path) -> None:
        """
        Open persistent Chroma. On failure (incl. Rust panic from bad sqlite),
        wipe this identity's directory once and retry — common when a Windows-built
        index is mounted into a Linux container.
        """
        import chromadb
        from chromadb.config import Settings

        index_dir = Path(index_dir).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        last_err: BaseException | None = None

        for attempt in range(2):
            try:
                self._close_chroma()
                settings = Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                )
                self._client = chromadb.PersistentClient(
                    path=str(index_dir),
                    settings=settings,
                )
                self._collection = self._client.get_or_create_collection(
                    name="docs",
                    metadata={"hnsw:space": "cosine"},
                )
                return
            except BaseException as exc:  # noqa: BLE001 — PanicException is BaseException
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                last_err = exc
                logger.error(
                    "Chroma open failed attempt=%s path=%s err=%s: %s",
                    attempt + 1,
                    index_dir,
                    type(exc).__name__,
                    exc,
                )
                self._close_chroma()
                if attempt == 0:
                    # Drop corrupt / incompatible store (e.g. host OS vs container)
                    shutil.rmtree(index_dir, ignore_errors=True)
                    index_dir.mkdir(parents=True, exist_ok=True)
                    continue
                break

        raise RuntimeError(
            f"Chroma open failed at {index_dir}: {type(last_err).__name__}: {last_err}. "
            "Delete data/rag/chroma/* and POST /v1/docs/reindex."
        )

    def _chunk_count(self) -> int:
        try:
            if self._collection is None:
                return 0
            return int(self._collection.count())
        except Exception:  # noqa: BLE001
            return 0

    def reindex(self, force: bool = True) -> dict[str, Any]:
        """Full rebuild of Chroma for the **current** embed identity."""
        del force  # always full rebuild for safety across dim/model
        with _lock:
            if not is_enabled():
                return {
                    "success": False,
                    "error": "RAG_ENABLED=false",
                    "error_code": "disabled",
                }

            t0 = time.time()
            warnings: list[str] = []
            try:
                from agents.embeddings import get_embed_identity, get_embeddings

                self._identity = get_embed_identity()
                self._embeddings = get_embeddings(self._identity)
                index_dir = self._index_dir()

                # Must close open clients BEFORE deleting the directory, otherwise
                # SQLite/Chroma returns "attempt to write a readonly database".
                self._close_chroma()
                if index_dir.exists():
                    shutil.rmtree(index_dir, ignore_errors=True)
                index_dir.mkdir(parents=True, exist_ok=True)

                self._open_chroma(index_dir)

                docs_dir = _docs_dir()
                files = discover_files(docs_dir)
                all_ids: list[str] = []
                all_docs: list[str] = []
                all_metas: list[dict[str, Any]] = []

                for fpath in files:
                    try:
                        units = load_file(fpath)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"{fpath.name}: load failed: {exc}")
                        continue
                    if not units:
                        warnings.append(f"{fpath.name}: no extractable text")
                        continue
                    for unit in units:
                        pieces = _split_text(
                            unit["text"], self.chunk_size, self.chunk_overlap
                        )
                        for idx, piece in enumerate(pieces):
                            cid = _chunk_id(
                                unit["source"], unit.get("page"), idx, piece
                            )
                            meta = {
                                "source": str(unit["source"]),
                                "path": str(unit.get("path") or fpath),
                                "page": unit.get("page")
                                if unit.get("page") is not None
                                else -1,
                                "file_type": str(unit.get("file_type") or ""),
                                "chunk_index": idx,
                            }
                            all_ids.append(cid)
                            all_docs.append(piece)
                            all_metas.append(meta)

                # Embed + add in batches
                total = len(all_docs)
                for start in range(0, total, max(1, self.batch_size)):
                    end = min(start + self.batch_size, total)
                    batch_docs = all_docs[start:end]
                    batch_ids = all_ids[start:end]
                    batch_metas = all_metas[start:end]
                    vectors = self._embeddings.embed_documents(batch_docs)
                    # Guard dim on first batch
                    if vectors and len(vectors[0]) != int(self._identity.dim):
                        actual = len(vectors[0])
                        logger.warning(
                            "Embedding dim mismatch config=%s actual=%s — "
                            "updating identity dim to actual for manifest",
                            self._identity.dim,
                            actual,
                        )
                        # Rebuild identity-like fields for manifest; path already chosen
                        # by configured dim — if actual differs, fail hard to avoid
                        # silent wrong geometry in HNSW.
                        raise RuntimeError(
                            f"Embedding dimension mismatch: config dim={self._identity.dim} "
                            f"but model returned {actual}. Set RAG_EMBED_DIM={actual} "
                            "and reindex."
                        )
                    self._collection.add(
                        ids=batch_ids,
                        documents=batch_docs,
                        metadatas=batch_metas,
                        embeddings=vectors,
                    )

                manifest = {
                    "provider": self._identity.provider,
                    "model": self._identity.model,
                    "dim": self._identity.dim,
                    "index_key": self._identity.index_key,
                    "chunk_size": self.chunk_size,
                    "chunk_overlap": self.chunk_overlap,
                    "built_at": _now_iso(),
                    "file_count": len(files),
                    "chunk_count": total,
                    "docs_dir": str(docs_dir),
                    "index_dir": str(index_dir),
                    "files": [p.name for p in files],
                }
                self._write_manifest(manifest)
                self._ready = True
                self._last_error = None
                elapsed = round(time.time() - t0, 2)
                logger.info(
                    "RAG reindex done files=%d chunks=%d dim=%d in %ss",
                    len(files),
                    total,
                    self._identity.dim,
                    elapsed,
                )
                return {
                    "success": True,
                    "file_count": len(files),
                    "chunk_count": total,
                    "elapsed_seconds": elapsed,
                    "warnings": warnings,
                    "identity": self._identity.as_dict(),
                    "index_dir": str(index_dir),
                    "manifest": manifest,
                }
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._ready = False
                logger.error("RAG reindex failed: %s", exc, exc_info=True)
                return {
                    "success": False,
                    "error": str(exc),
                    "error_code": "reindex_failed",
                    "warnings": warnings,
                }

    def readiness(self) -> dict[str, Any]:
        from agents.embeddings import get_embed_identity

        try:
            ident = self._identity or get_embed_identity()
            ident_d = ident.as_dict()
        except Exception as exc:  # noqa: BLE001
            ident_d = {"error": str(exc)}
            ident = None

        m = self._manifest or self._read_manifest()
        match = False
        try:
            match = self._identity_matches_manifest() if m else False
        except Exception:  # noqa: BLE001
            match = False

        return {
            "ready": bool(self._ready and is_enabled()),
            "enabled": is_enabled(),
            "backend": "rag_chroma",
            "agent": self.name,
            "identity": ident_d,
            "identity_matches_index": match,
            "docs_dir": str(_docs_dir()),
            "chroma_root": str(_chroma_root()),
            "index_dir": str(self._index_dir()) if ident else None,
            "chunk_count": self._chunk_count(),
            "top_k": self.top_k,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "manifest": m or None,
            "error": self._last_error,
            "supported_types": sorted(_SUPPORTED_SUFFIXES),
        }

    def list_files(self) -> dict[str, Any]:
        docs_dir = _docs_dir()
        files = discover_files(docs_dir)
        m = self._manifest or self._read_manifest()
        indexed = set(m.get("files") or [])
        return {
            "docs_dir": str(docs_dir),
            "files": [
                {
                    "name": p.name,
                    "path": str(p),
                    "suffix": p.suffix.lower(),
                    "size_bytes": p.stat().st_size if p.is_file() else 0,
                    "indexed_in_current_manifest": p.name in indexed,
                }
                for p in files
            ],
            "count": len(files),
        }

    def stats(self) -> dict[str, Any]:
        return self.readiness()

    def chat(self, message: str) -> dict[str, Any]:
        """Answer from retrieved chunks only. Never raises to callers."""
        try:
            message = (message or "").strip()
            if not message:
                return {
                    "success": False,
                    "response": None,
                    "error": "message must not be empty",
                    "error_code": "validation",
                    "sources": [],
                }

            if not is_enabled():
                return {
                    "success": False,
                    "response": None,
                    "error": "RAG disabled (RAG_ENABLED=false)",
                    "error_code": "disabled",
                    "sources": [],
                }

            if not self._ready or self._embeddings is None or self._collection is None:
                self.initialize()
            if not self._ready or self._embeddings is None or self._collection is None:
                return {
                    "success": False,
                    "response": None,
                    "error": self._last_error or "RAG agent not ready",
                    "error_code": "not_ready",
                    "sources": [],
                }

            if self._manifest and not self._identity_matches_manifest():
                return {
                    "success": False,
                    "response": None,
                    "error": self._last_error
                    or "Embed identity mismatch — run POST /v1/docs/reindex",
                    "error_code": "identity_mismatch",
                    "sources": [],
                    "retryable": False,
                }

            count = self._chunk_count()
            if count <= 0:
                return {
                    "success": False,
                    "response": (
                        "Hujjat indeksi bo'sh. PDF/Word fayllarni data/docs ga "
                        "qo'ying va POST /v1/docs/reindex chaqiring."
                    ),
                    "error": "empty_index",
                    "error_code": "empty_index",
                    "sources": [],
                }

            q_vec = self._embeddings.embed_query(message)
            if len(q_vec) != int(self._identity.dim):
                return {
                    "success": False,
                    "response": None,
                    "error": (
                        f"Query embedding dim {len(q_vec)} != config {self._identity.dim}"
                    ),
                    "error_code": "dim_mismatch",
                    "sources": [],
                }

            k = max(1, min(self.top_k, count))
            result = self._collection.query(
                query_embeddings=[q_vec],
                n_results=k,
                include=["documents", "metadatas", "distances"],
            )

            docs = (result.get("documents") or [[]])[0]
            metas = (result.get("metadatas") or [[]])[0]
            dists = (result.get("distances") or [[]])[0]

            sources: list[dict[str, Any]] = []
            context_blocks: list[str] = []
            for i, doc in enumerate(docs):
                meta = metas[i] if i < len(metas) else {}
                dist = dists[i] if i < len(dists) else None
                # cosine distance → similarity-ish score
                score = None
                if dist is not None:
                    try:
                        score = round(1.0 - float(dist), 4)
                    except (TypeError, ValueError):
                        score = None
                page = meta.get("page")
                if page == -1:
                    page = None
                src = {
                    "file": meta.get("source"),
                    "page": page,
                    "score": score,
                    "excerpt": (doc or "")[:400],
                    "file_type": meta.get("file_type"),
                }
                sources.append(src)
                page_s = f" p.{page}" if page is not None else ""
                context_blocks.append(
                    f"[{i + 1}] source={meta.get('source')}{page_s}\n{doc}"
                )

            if not context_blocks:
                return {
                    "success": True,
                    "response": (
                        "Indekslangan hujjatlardan mos parcha topilmadi. "
                        "Boshqa savol bering yoki hujjatlarni yangilab reindex qiling."
                    ),
                    "sources": [],
                    "backend": "rag",
                    "embed_provider": self._identity.provider,
                    "embed_model": self._identity.model,
                    "embed_dim": self._identity.dim,
                    "agents_used": ["rag_agent"],
                }

            answer = self._generate_answer(message, "\n\n".join(context_blocks))
            return {
                "success": True,
                "response": answer,
                "error": None,
                "sources": sources,
                "backend": "rag",
                "embed_provider": self._identity.provider,
                "embed_model": self._identity.model,
                "embed_dim": self._identity.dim,
                "agents_used": ["rag_agent"],
                "mode": "rag_chroma",
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("rag_agent.chat failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "response": None,
                "error": f"RAG agent error: {exc}",
                "error_code": "agent_error",
                "retryable": True,
                "sources": [],
            }

    def _generate_answer(self, question: str, context: str) -> str:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        model = (
            _env("RAG_LLM_MODEL")
            or _env("LLM_MODEL")
            or _env("OPENAI_MODEL")
            or "gpt-4.1"
        )
        api_key = (
            _env("OPENAI_API_KEY") or _env("LLM_API_KEY") or _env("HERMES_API_KEY")
        )
        base_url = _env("OPENAI_BASE_URL") or None
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "temperature": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        llm = ChatOpenAI(**kwargs)

        human = (
            f"## Retrieved document context\n\n{context}\n\n"
            f"## User question\n\n{question}\n\n"
            "Answer using only the context above."
        )
        msg = llm.invoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=human),
            ]
        )
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", b)))
                else:
                    parts.append(str(b))
            return " ".join(parts).strip()
        return str(content if content is not None else msg).strip()


def get_rag_agent() -> RAGAgentService:
    global _service
    with _lock:
        if _service is None:
            _service = RAGAgentService()
            if is_enabled():
                try:
                    _service.initialize()
                except BaseException as exc:  # noqa: BLE001
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    _service._ready = False
                    _service._last_error = f"{type(exc).__name__}: {exc}"
                    logger.error(
                        "get_rag_agent initialize suppressed: %s",
                        _service._last_error,
                    )
        elif is_enabled() and not _service.ready:
            try:
                _service.initialize()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                _service._ready = False
                _service._last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "get_rag_agent re-init suppressed: %s", _service._last_error
                )
        return _service
