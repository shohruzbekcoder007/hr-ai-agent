"""
App-level global self-improving layer for the SQL agent.

Idea (backend-agnostic — works with hermes, hermes_lite or plain LangGraph):

    successful question → executed SQL  ==> stored as a reusable "recipe"
    new question ==> retrieve top-k similar recipes ==> inject as few-shot
                     examples into the SQL agent prompt.

Design constraints:

* GLOBAL, not per-user: a single shared store benefits everyone hitting this
  instance (one shared database → the same SQL recipes / multi-script term
  mappings help all users). See README "self-improving".
* No prompt bloat: only the top-k relevant recipes are injected (retrieval),
  the full library lives outside the prompt. The store itself is bounded
  (``SELF_IMPROVE_MAX_RECIPES``) and pruned by least-used → curation, not
  unbounded accumulation.
* Leak-safe: we store the *SQL technique* (query text) and the question, never
  the returned result rows (which could contain other users' HR data).
* Dependency-free retrieval: lexical token overlap (handles Cyrillic / Latin /
  English), so it is testable without an LLM, DB or embedding service.

Everything here is best-effort: failures never propagate to the chat path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("self_improve")

_lock = threading.RLock()
_store: Optional["RecipeStore"] = None


# ---------------------------------------------------------------------------
# env helpers (mirrors the tiny helpers used elsewhere in agents/)
# ---------------------------------------------------------------------------

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
    return _env_bool("SELF_IMPROVE_ENABLED", True)


def _default_store_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "self_improve.json"


def _store_path() -> Path:
    return Path(_env("SELF_IMPROVE_STORE_PATH") or str(_default_store_path()))


# ---------------------------------------------------------------------------
# tokenization & scoring (Cyrillic + Latin + English aware, no dependencies)
# ---------------------------------------------------------------------------

# Keep small: high-frequency, low-signal words across uz / ru / en.
_STOPWORDS = {
    "the", "and", "for", "with", "how", "many", "much", "what", "which", "who",
    "list", "show", "give", "count", "number", "all", "from", "into", "are",
    "nechta", "nechi", "qancha", "qanday", "kim", "nima", "bor", "royxat",
    "royxati", "va", "ular", "shu", "uchun", "bilan", "haqida", "bo", "yoki",
    "сколько", "что", "как", "кто", "все", "или", "для", "это",
}

_TOKEN_RE = re.compile(r"[0-9A-Za-zЀ-ӿʻʼ']+")


def tokenize(text: str) -> set[str]:
    """Lowercase word tokens; keeps Cyrillic, drops tiny/stop tokens."""
    if not text:
        return set()
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        tok = tok.strip("'ʻʼ")
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


def similarity(query_tokens: set[str], recipe_tokens: set[str]) -> float:
    """
    Fraction of the query covered by the recipe, with a mild boost for
    recipes that are themselves focused (overlap coefficient blend).
    Range ~0..1. Cheap and good enough for few-shot selection.
    """
    if not query_tokens or not recipe_tokens:
        return 0.0
    common = query_tokens & recipe_tokens
    if not common:
        return 0.0
    coverage = len(common) / len(query_tokens)
    focus = len(common) / len(recipe_tokens)
    return 0.7 * coverage + 0.3 * focus


# ---------------------------------------------------------------------------
# SQL extraction from tool traces
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b",
    re.IGNORECASE,
)


def _looks_like_select(sql: str) -> bool:
    s = (sql or "").lstrip().lower()
    if not s.startswith(("select", "with")):
        return False
    return _WRITE_RE.search(sql or "") is None


def extract_sql(tools_called: list[dict[str, Any]] | None) -> str | None:
    """
    Return the last successfully-issued read-only SQL statement from a
    ``tools_called`` trace (see sql_agent._extract_tools_from_messages).
    Only SELECT/CTE queries are kept — techniques, never writes.
    """
    if not tools_called:
        return None
    found: str | None = None
    for entry in tools_called:
        if not isinstance(entry, dict):
            continue
        ti = entry.get("tool_input")
        candidate: str | None = None
        if isinstance(ti, dict):
            for key in ("query", "sql", "__arg1", "input"):
                val = ti.get(key)
                if isinstance(val, str) and val.strip():
                    candidate = val
                    break
        elif isinstance(ti, str) and ti.strip():
            candidate = ti
        if candidate and _looks_like_select(candidate):
            found = candidate.strip()  # keep the LAST valid one
    if found and len(found) > 2000:
        found = found[:2000]
    return found


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).strip()


class RecipeStore:
    """
    Bounded, thread-safe JSON-backed store of question→SQL recipes.

    A recipe: {id, question, question_tokens, sql, tags, uses, created_at,
               updated_at}. ``tags`` are extra keywords for retrieval.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _store_path()
        self.max_recipes = _env_int("SELF_IMPROVE_MAX_RECIPES", 500)
        self.top_k = _env_int("SELF_IMPROVE_TOP_K", 3)
        self.min_score = float(_env("SELF_IMPROVE_MIN_SCORE") or "0.18")
        self.max_question = _env_int("SELF_IMPROVE_MAX_QUESTION_CHARS", 500)
        self._recipes: list[dict[str, Any]] = []
        self._loaded = False

    # ---- persistence -----------------------------------------------------

    def load(self) -> None:
        with _lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                if self.path.is_file():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    recipes = raw.get("recipes") if isinstance(raw, dict) else raw
                    if isinstance(recipes, list):
                        self._recipes = [r for r in recipes if isinstance(r, dict)]
                    logger.info(
                        "self_improve store loaded: %d recipes from %s",
                        len(self._recipes),
                        self.path,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("self_improve load failed (%s): %s", self.path, exc)
                self._recipes = []

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "recipes": self._recipes}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("self_improve save failed (%s): %s", self.path, exc)

    # ---- write path ------------------------------------------------------

    def add(self, question: str, sql: str, tags: list[str] | None = None) -> bool:
        """
        Store a successful question→SQL recipe. De-dupes on identical SQL or
        near-identical question; prunes least-used when over capacity.
        Returns True if the store changed.
        """
        question = (question or "").strip()[: self.max_question]
        sql = (sql or "").strip()
        if not question or not sql or not _looks_like_select(sql):
            return False

        self.load()
        norm = _norm_sql(sql)
        q_tokens = tokenize(question)
        with _lock:
            for r in self._recipes:
                same_sql = _norm_sql(r.get("sql", "")) == norm
                same_q = set(r.get("question_tokens") or []) == q_tokens and q_tokens
                if same_sql or same_q:
                    r["uses"] = int(r.get("uses", 1)) + 1
                    r["updated_at"] = _now_iso()
                    # keep the freshest working SQL for this question
                    if same_q and not same_sql:
                        r["sql"] = sql
                    self._save_locked()
                    return True

            self._recipes.append(
                {
                    "id": _now_iso() + "-" + str(len(self._recipes) + 1),
                    "question": question,
                    "question_tokens": sorted(q_tokens),
                    "sql": sql,
                    "tags": list(tags or []),
                    "uses": 1,
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            self._prune_locked()
            self._save_locked()
            logger.info(
                "self_improve: learned recipe (total=%d) q=%r",
                len(self._recipes),
                question[:80],
            )
            return True

    def _prune_locked(self) -> None:
        if len(self._recipes) <= self.max_recipes:
            return
        # keep most-used, then most-recently-updated
        self._recipes.sort(
            key=lambda r: (int(r.get("uses", 1)), r.get("updated_at", "")),
            reverse=True,
        )
        dropped = len(self._recipes) - self.max_recipes
        self._recipes = self._recipes[: self.max_recipes]
        logger.info("self_improve: pruned %d low-use recipes", dropped)

    # ---- read path -------------------------------------------------------

    def retrieve(self, question: str, k: int | None = None) -> list[dict[str, Any]]:
        """Top-k recipes most similar to ``question`` (above min_score)."""
        self.load()
        q_tokens = tokenize(question)
        if not q_tokens:
            return []
        k = self.top_k if k is None else k
        with _lock:
            scored: list[tuple[float, dict[str, Any]]] = []
            for r in self._recipes:
                r_tokens = set(r.get("question_tokens") or []) | tokenize(
                    " ".join(r.get("tags") or [])
                )
                score = similarity(q_tokens, r_tokens)
                if score >= self.min_score:
                    scored.append((score, r))
            scored.sort(key=lambda x: (x[0], int(x[1].get("uses", 1))), reverse=True)
            return [r for _, r in scored[:k]]

    def stats(self) -> dict[str, Any]:
        self.load()
        with _lock:
            top = sorted(
                self._recipes,
                key=lambda r: int(r.get("uses", 1)),
                reverse=True,
            )[:5]
            return {
                "enabled": is_enabled(),
                "count": len(self._recipes),
                "path": str(self.path),
                "top_k": self.top_k,
                "min_score": self.min_score,
                "max_recipes": self.max_recipes,
                "top_recipes": [
                    {"question": r.get("question"), "uses": r.get("uses", 1)}
                    for r in top
                ],
            }


def get_store() -> RecipeStore:
    global _store
    with _lock:
        if _store is None:
            _store = RecipeStore()
            _store.load()
        return _store


# ---------------------------------------------------------------------------
# prompt integration
# ---------------------------------------------------------------------------

def format_recipes_for_prompt(recipes: list[dict[str, Any]]) -> str:
    """Render retrieved recipes as a bounded few-shot block for the SQL agent."""
    if not recipes:
        return ""
    lines = [
        "[LEARNED SQL PATTERNS — previously successful on THIS database]",
        "Similar questions were answered before. Reuse or adapt the SQL below "
        "when relevant, but always re-validate against the live schema and "
        "re-execute — do not trust them blindly.",
        "",
    ]
    for i, r in enumerate(recipes, 1):
        q = str(r.get("question", "")).strip()
        sql = str(r.get("sql", "")).strip()
        lines.append(f"{i}. Q: {q}")
        lines.append("   SQL:")
        lines.append("   " + sql.replace("\n", "\n   "))
        lines.append("")
    return "\n".join(lines).rstrip()


def augment_prompt(full_input: str, question: str) -> str:
    """Append relevant learned patterns to a prepared prompt (best-effort)."""
    if not is_enabled():
        return full_input
    try:
        recipes = get_store().retrieve(question)
        block = format_recipes_for_prompt(recipes)
        if block:
            return full_input.rstrip() + "\n\n" + block
    except Exception as exc:  # noqa: BLE001
        logger.debug("augment_prompt skipped: %s", exc)
    return full_input


def learn_from_result(question: str, result: dict[str, Any]) -> None:
    """Capture a successful question→SQL recipe from a chat result (best-effort)."""
    if not is_enabled():
        return
    try:
        if not result or not result.get("success"):
            return
        sql = extract_sql(result.get("tools_called"))
        if sql:
            get_store().add(question, sql)
    except Exception as exc:  # noqa: BLE001
        logger.debug("learn_from_result skipped: %s", exc)


def stats() -> dict[str, Any]:
    try:
        return get_store().stats()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": is_enabled(), "error": str(exc)}
