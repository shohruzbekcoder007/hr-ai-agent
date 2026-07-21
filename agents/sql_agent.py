"""
LangChain SQLAgent — mirrors Langflow graph:

  Chat Input → Prompt Template → SQLAgent → Chat Output

All public methods catch errors and return structured results (no uncaught crashes).
Rate limits (429) are retried automatically.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("sql_agent")

_agent_lock = threading.RLock()
_service: Optional["SQLAgentService"] = None


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


def _redact_url(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.password:
            return url
        netloc = p.netloc.replace(f":{p.password}@", ":***@")
        return p._replace(netloc=netloc).geturl()
    except Exception:  # noqa: BLE001
        return "<unparseable>"


def _sqlalchemy_url(url: str) -> str:
    """
    Prefer psycopg v3 driver (package ``psycopg``).

    Plain postgresql:// makes SQLAlchemy look for psycopg2, which is not installed
    in our image → ModuleNotFoundError: psycopg2 (Gateway Error in Open WebUI).
    """
    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("postgresql+psycopg://") or u.startswith("postgresql+psycopg2://"):
        return u
    if u.startswith("postgresql://"):
        return "postgresql+psycopg://" + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return "postgresql+psycopg://" + u[len("postgres://") :]
    return u


def _default_prompt_path() -> Path:
    return Path(__file__).resolve().parent.parent / "prompts" / "sql_agent_system.md"


def load_system_prompt() -> str:
    path = Path(_env("SYSTEM_PROMPT_PATH") or str(_default_prompt_path()))
    if not path.is_file():
        raise FileNotFoundError(f"System prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _format_user_prompt(template: str, user_message: str) -> str:
    if "{input}" in template:
        return template.replace("{input}", user_message)
    return (
        template.rstrip()
        + "\n\n------------------------------------------------------------\n\n"
        + "User Question:\n\n"
        + user_message
    )


# ---------------------------------------------------------------------------
# Error classification & retries
# ---------------------------------------------------------------------------

def _error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        parts.append(str(cause))
    # openai nested body
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(str(body))
    return " ".join(parts)


def _is_rate_limit(exc: BaseException) -> bool:
    text = _error_text(exc).lower()
    name = type(exc).__name__.lower()
    code = str(getattr(exc, "status_code", "") or getattr(exc, "code", "") or "")
    return (
        "rate_limit" in text
        or "rate limit" in text
        or "tokens per min" in text
        or "429" in text
        or code == "429"
        or "ratelimit" in name
    )


def _is_auth_error(exc: BaseException) -> bool:
    text = _error_text(exc).lower()
    code = str(getattr(exc, "status_code", "") or "")
    return (
        code in {"401", "403"}
        or "invalid_api_key" in text
        or "incorrect api key" in text
        or "authentication" in text
        or "unauthorized" in text
    )


def _is_retryable(exc: BaseException) -> bool:
    if _is_rate_limit(exc):
        return True
    text = _error_text(exc).lower()
    name = type(exc).__name__.lower()
    code = str(getattr(exc, "status_code", "") or "")
    return (
        code in {"408", "409", "425", "429", "500", "502", "503", "504"}
        or "timeout" in text
        or "timed out" in text
        or "connection" in text
        or "temporarily" in text
        or "overloaded" in text
        or "service unavailable" in text
        or "apiconnection" in name
        or "timeout" in name
    )


def _retry_delay_seconds(exc: BaseException, attempt: int) -> float:
    """Parse OpenAI 'try again in 318ms' or use exponential backoff."""
    text = _error_text(exc)
    m = re.search(r"try again in\s+([\d.]+)\s*ms", text, re.I)
    if m:
        return max(0.4, float(m.group(1)) / 1000.0 + 0.25)
    m = re.search(r"try again in\s+([\d.]+)\s*s", text, re.I)
    if m:
        return max(0.5, float(m.group(1)) + 0.1)
    # exponential: 1, 2, 4 ...
    return min(30.0, (2 ** max(0, attempt)) * 0.75)


def _friendly_error(exc: BaseException) -> dict[str, Any]:
    """User-facing message; never leak raw stack traces."""
    raw = _error_text(exc)
    if _is_rate_limit(exc):
        delay = _retry_delay_seconds(exc, 0)
        return {
            "error_code": "rate_limit",
            "error": (
                "OpenAI rate limit (TPM/RPM) oshib ketdi. "
                f"Iltimos {delay:.1f}s dan keyin qayta urinib ko'ring."
            ),
            "error_detail": raw[:500],
            "retryable": True,
        }
    if _is_auth_error(exc):
        return {
            "error_code": "auth",
            "error": (
                "LLM API kaliti noto'g'ri yoki ruxsat yo'q. "
                "OPENAI_API_KEY ni tekshiring."
            ),
            "error_detail": raw[:500],
            "retryable": False,
        }
    text_l = raw.lower()
    if "database" in text_l or "postgres" in text_l or "connection refused" in text_l:
        return {
            "error_code": "database",
            "error": (
                "Ma'lumotlar bazasiga ulanib bo'lmadi. "
                "DATABASE_URL va tarmoqni tekshiring."
            ),
            "error_detail": raw[:500],
            "retryable": True,
        }
    if "not ready" in text_l or "not set" in text_l:
        return {
            "error_code": "config",
            "error": str(exc),
            "error_detail": raw[:500],
            "retryable": False,
        }
    return {
        "error_code": "agent_error",
        "error": (
            "Agent so'rovni bajarolmadi. Biroz kutib qayta urinib ko'ring. "
            f"({type(exc).__name__})"
        ),
        "error_detail": raw[:800],
        "retryable": _is_retryable(exc),
    }


def _invoke_with_retry(
    fn: Callable[[], Any],
    *,
    max_retries: int,
    what: str = "call",
) -> Any:
    """Run fn(); retry on rate-limit / transient errors."""
    last_exc: BaseException | None = None
    attempts = max(1, max_retries + 1)
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable(exc) or attempt >= attempts - 1:
                raise
            delay = _retry_delay_seconds(exc, attempt)
            logger.warning(
                "%s failed (attempt %s/%s, %s): %s — retry in %.2fs",
                what,
                attempt + 1,
                attempts,
                type(exc).__name__,
                str(exc)[:200],
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _import_create_sql_agent():
    errors: list[str] = []
    for path in (
        "langchain_community.agent_toolkits.sql.base",
        "langchain_community.agent_toolkits",
    ):
        try:
            mod = __import__(path, fromlist=["create_sql_agent"])
            fn = getattr(mod, "create_sql_agent", None)
            if fn is not None:
                return fn
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
    raise ImportError("create_sql_agent not found: " + " | ".join(errors))


def _final_text_from_messages(messages: list[Any]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "\n".join(parts).strip()
    return str(content or last).strip()


def _extract_tools_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for msg in messages or []:
        try:
            name = getattr(msg, "name", None) or (
                msg.get("name") if isinstance(msg, dict) else None
            )
            msg_type = getattr(msg, "type", None) or (
                msg.get("type") if isinstance(msg, dict) else None
            )
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            preview = str(content)[:400] if content is not None else None
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tools.append(
                            {
                                "tool": tc.get("name"),
                                "tool_input": tc.get("args") or tc.get("arguments"),
                            }
                        )
                    else:
                        tools.append(
                            {
                                "tool": getattr(tc, "name", None),
                                "tool_input": getattr(tc, "args", None),
                            }
                        )
            elif msg_type == "tool" or (
                isinstance(msg, dict) and msg.get("role") == "tool"
            ):
                tools.append({"tool": name, "observation_preview": preview})
        except Exception:  # noqa: BLE001
            continue
    return tools


def _build_sql_tools(db: Any, llm: Any) -> list[Any]:
    """List / schema / query tools. Checker is optional (silent skip)."""
    from langchain_community.tools.sql_database.tool import (
        InfoSQLDatabaseTool,
        ListSQLDatabaseTool,
    )

    try:
        from langchain_community.tools import QuerySQLDatabaseTool as QueryTool
    except Exception:  # noqa: BLE001
        from langchain_community.tools.sql_database.tool import (  # type: ignore
            QuerySQLDataBaseTool as QueryTool,
        )

    tools: list[Any] = [
        ListSQLDatabaseTool(db=db),
        InfoSQLDatabaseTool(db=db),
        QueryTool(db=db),
    ]
    try:
        from langchain_community.tools.sql_database.tool import QuerySQLCheckerTool

        tools.append(QuerySQLCheckerTool(db=db, llm=llm))
    except Exception as exc:  # noqa: BLE001
        # Optional tool — do not scare operators with warnings on every start
        logger.debug("QuerySQLCheckerTool unavailable (optional): %s", exc)
    return tools


def _fail_result(
    *,
    error: str,
    error_code: str = "agent_error",
    error_detail: str | None = None,
    retryable: bool = False,
    backend: str | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "response": None,
        "error": error,
        "error_code": error_code,
        "error_detail": error_detail,
        "retryable": retryable,
        "backend": backend,
        "tools_called": [],
        "tool_call_count": 0,
    }


class SQLAgentService:
    """Process-wide SQL agent service. Public API never raises to callers."""

    def __init__(self) -> None:
        self.database_url = _env("DATABASE_URL") or _env("SQL_DATABASE_URI")
        self.model_name = _env("LLM_MODEL") or _env("OPENAI_MODEL") or "gpt-4.1"
        self.api_key = _env("OPENAI_API_KEY") or _env("LLM_API_KEY")
        self.base_url = _env("OPENAI_BASE_URL") or None
        self.max_iterations = _env_int("SQL_AGENT_MAX_ITERATIONS", 15)
        self.max_retries = _env_int("SQL_AGENT_MAX_RETRIES", 3)
        self.verbose = _env_bool("SQL_AGENT_VERBOSE", True)
        self.handle_parsing_errors = _env_bool(
            "SQL_AGENT_HANDLE_PARSING_ERRORS", True
        )
        try:
            self.prompt_template = load_system_prompt()
        except Exception as exc:  # noqa: BLE001
            self.prompt_template = (
                "You are an expert PostgreSQL SQL Agent.\n\nUser Question:\n\n{input}"
            )
            logger.error("Failed to load system prompt: %s", exc)
        self._backend: str | None = None
        self._executor: Any = None
        self._db: Any = None
        self._last_error: str | None = None
        self._ready = False

    def initialize(self) -> dict[str, Any]:
        """
        Build agent. On failure sets ready=False and records error;
        does not crash the process. Returns readiness dict.
        """
        with _agent_lock:
            if self._ready and self._executor is not None:
                return self.readiness()

            if not self.database_url:
                self._last_error = "DATABASE_URL is not set"
                self._ready = False
                return self.readiness()
            if not self.api_key:
                self._last_error = "OPENAI_API_KEY is not set"
                self._ready = False
                return self.readiness()

            try:
                from langchain_community.utilities import SQLDatabase
                from langchain_openai import ChatOpenAI

                llm_kwargs: dict[str, Any] = {
                    "model": self.model_name,
                    "api_key": self.api_key,
                    "temperature": 0,
                }
                if self.base_url:
                    llm_kwargs["base_url"] = self.base_url

                llm = ChatOpenAI(**llm_kwargs)

                def _connect_db() -> Any:
                    return SQLDatabase.from_uri(
                        _sqlalchemy_url(self.database_url),
                        sample_rows_in_table_info=0,
                    )

                self._db = _invoke_with_retry(
                    _connect_db,
                    max_retries=min(2, self.max_retries),
                    what="database_connect",
                )
                tools = _build_sql_tools(self._db, llm)

                try:
                    from langgraph.prebuilt import create_react_agent

                    self._executor = create_react_agent(llm, tools)
                    self._backend = "langgraph"
                    logger.info(
                        "SQLAgent backend=langgraph tools=%s",
                        [getattr(t, "name", type(t).__name__) for t in tools],
                    )
                except Exception as lg_exc:  # noqa: BLE001
                    logger.warning(
                        "langgraph unavailable (%s); trying create_sql_agent",
                        lg_exc,
                    )
                    try:
                        from langchain_community.agent_toolkits import (
                            SQLDatabaseToolkit,
                        )

                        toolkit = SQLDatabaseToolkit(db=self._db, llm=llm)
                        create_sql_agent = _import_create_sql_agent()
                        try:
                            self._executor = create_sql_agent(
                                llm=llm,
                                toolkit=toolkit,
                                verbose=self.verbose,
                                max_iterations=self.max_iterations,
                                agent_executor_kwargs={
                                    "handle_parsing_errors": self.handle_parsing_errors,
                                },
                            )
                        except TypeError:
                            self._executor = create_sql_agent(
                                llm=llm,
                                toolkit=toolkit,
                                verbose=self.verbose,
                                max_iterations=self.max_iterations,
                            )
                        self._backend = "create_sql_agent"
                        logger.info("SQLAgent backend=create_sql_agent")
                    except Exception as sql_exc:  # noqa: BLE001
                        self._ready = False
                        self._executor = None
                        self._backend = None
                        self._last_error = str(sql_exc)
                        logger.error("SQLAgent build failed: %s", sql_exc)
                        return self.readiness()

                try:
                    _ = self._db.get_usable_table_names()
                except Exception as probe_exc:  # noqa: BLE001
                    self._ready = False
                    self._last_error = f"DB probe failed: {probe_exc}"
                    logger.error("%s", self._last_error)
                    return self.readiness()

                self._ready = True
                self._last_error = None
                logger.info(
                    "SQLAgent ready backend=%s model=%s db=%s max_iterations=%s",
                    self._backend,
                    self.model_name,
                    _redact_url(self.database_url),
                    self.max_iterations,
                )
            except Exception as exc:  # noqa: BLE001
                self._ready = False
                self._executor = None
                self._backend = None
                info = _friendly_error(exc)
                self._last_error = info["error"]
                logger.error(
                    "SQLAgent initialize failed [%s]: %s",
                    info.get("error_code"),
                    info.get("error_detail") or exc,
                )
                # do not re-raise — callers use readiness()

            return self.readiness()

    @property
    def ready(self) -> bool:
        return self._ready and self._executor is not None

    def readiness(self) -> dict[str, Any]:
        tables: list[str] = []
        if self._db is not None:
            try:
                tables = list(self._db.get_usable_table_names())
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                # keep ready if executor exists; probe flake is soft
                logger.warning("table list probe failed: %s", exc)
        return {
            "ready": self.ready,
            "backend": self._backend,
            "model": self.model_name,
            "database_url_configured": bool(self.database_url),
            "database_url_redacted": _redact_url(self.database_url)
            if self.database_url
            else None,
            "api_key_configured": bool(self.api_key),
            "max_iterations": self.max_iterations,
            "max_retries": self.max_retries,
            "table_count": len(tables),
            "tables_sample": tables[:20],
            "error": self._last_error,
            "agent_type": "langchain-sql-agent",
            "flow": "Chat Input → Prompt Template → SQLAgent → Chat Output",
        }

    def _run_once(self, full_input: str) -> dict[str, Any]:
        tools_called: list[dict[str, Any]] = []
        if self._backend == "langgraph":
            recursion = max(10, self.max_iterations * 3)
            result = self._executor.invoke(
                {"messages": [("user", full_input)]},
                config={"recursion_limit": recursion},
            )
            messages = result.get("messages") if isinstance(result, dict) else None
            output = _final_text_from_messages(list(messages or []))
            tools_called = _extract_tools_from_messages(list(messages or []))
        else:
            result = self._executor.invoke({"input": full_input})
            if isinstance(result, dict):
                output = (
                    result.get("output") or result.get("result") or str(result)
                )
                intermediate = result.get("intermediate_steps") or []
                for step in intermediate:
                    try:
                        action, observation = step
                        tools_called.append(
                            {
                                "tool": getattr(action, "tool", None) or str(action),
                                "tool_input": getattr(action, "tool_input", None),
                                "observation_preview": str(observation)[:400],
                            }
                        )
                    except Exception:  # noqa: BLE001
                        tools_called.append({"raw": str(step)[:400]})
            else:
                output = str(result)

        if not (output or "").strip():
            return {
                "success": False,
                "response": None,
                "error": "Agent bo'sh javob qaytardi.",
                "error_code": "empty_response",
                "retryable": True,
                "backend": self._backend,
                "tools_called": tools_called,
                "tool_call_count": len(tools_called),
            }

        return {
            "success": True,
            "response": output,
            "error": None,
            "error_code": None,
            "error_detail": None,
            "retryable": False,
            "backend": self._backend,
            "tools_called": tools_called,
            "tool_call_count": len(tools_called),
        }

    def chat(self, message: str) -> dict[str, Any]:
        """
        Always returns a dict. Never raises to the HTTP / CLI layer.
        Retries on rate-limit and other transient LLM errors.
        """
        try:
            message = (message or "").strip()
            if not message:
                return _fail_result(
                    error="message must not be empty",
                    error_code="validation",
                    backend=self._backend,
                )

            if not self.ready:
                self.initialize()
            if not self.ready:
                return _fail_result(
                    error=self._last_error or "SQLAgent not ready",
                    error_code="not_ready",
                    backend=self._backend,
                )

            try:
                full_input = _format_user_prompt(self.prompt_template, message)
            except Exception as exc:  # noqa: BLE001
                info = _friendly_error(exc)
                return _fail_result(
                    error=info["error"],
                    error_code=info["error_code"],
                    error_detail=info.get("error_detail"),
                    backend=self._backend,
                )

            logger.info(
                "SQLAgent chat backend=%s message_len=%d",
                self._backend,
                len(message),
            )

            try:
                return _invoke_with_retry(
                    lambda: self._run_once(full_input),
                    max_retries=self.max_retries,
                    what="sql_agent_chat",
                )
            except Exception as exc:  # noqa: BLE001
                info = _friendly_error(exc)
                # rate limit / transient: warning only (no huge stack spam)
                if info.get("retryable"):
                    logger.warning(
                        "SQLAgent chat failed after retries [%s]: %s",
                        info.get("error_code"),
                        info.get("error_detail") or exc,
                    )
                else:
                    logger.error(
                        "SQLAgent chat failed [%s]: %s",
                        info.get("error_code"),
                        info.get("error_detail") or exc,
                        exc_info=logger.isEnabledFor(logging.DEBUG),
                    )
                return _fail_result(
                    error=info["error"],
                    error_code=info["error_code"],
                    error_detail=info.get("error_detail"),
                    retryable=bool(info.get("retryable")),
                    backend=self._backend,
                )
        except Exception as exc:  # noqa: BLE001 — absolute last resort
            logger.error("SQLAgent chat unexpected failure: %s", exc, exc_info=True)
            info = _friendly_error(exc)
            return _fail_result(
                error=info["error"],
                error_code=info.get("error_code", "agent_error"),
                error_detail=info.get("error_detail"),
                retryable=bool(info.get("retryable")),
                backend=self._backend,
            )


def get_sql_agent() -> SQLAgentService:
    """Singleton; never raises."""
    global _service
    with _agent_lock:
        if _service is None:
            try:
                _service = SQLAgentService()
                _service.initialize()
            except Exception as exc:  # noqa: BLE001
                logger.error("get_sql_agent construct failed: %s", exc)
                _service = SQLAgentService()
                _service._last_error = str(exc)
        elif not _service.ready:
            try:
                _service.initialize()
            except Exception as exc:  # noqa: BLE001
                logger.error("SQLAgent re-init failed: %s", exc)
        return _service
