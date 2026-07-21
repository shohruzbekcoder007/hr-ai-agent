"""
Process entrypoint for the LangChain SQLAgent service.

Usage:
  python -m app.main
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    _bootstrap_paths()

    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.is_file():
            load_dotenv(env_file)
    except Exception:
        pass

    from app.logging_setup import setup_logging

    setup_logging()
    logger = logging.getLogger("app")

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    workers = int(os.getenv("API_WORKERS", "1"))
    reload = os.getenv("API_RELOAD", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info("=" * 60)
    logger.info("LangChain SQLAgent — server starting")
    logger.info("Host=%s Port=%s Workers=%s Reload=%s", host, port, workers, reload)
    logger.info("LLM_MODEL=%s", os.getenv("LLM_MODEL", "gpt-4.1"))
    logger.info(
        "DATABASE_URL configured=%s",
        bool(os.getenv("DATABASE_URL") or os.getenv("SQL_DATABASE_URI")),
    )
    logger.info("=" * 60)

    try:
        from agents.sql_agent import get_sql_agent

        agent = get_sql_agent()
        logger.info("SQLAgent readiness: %s", agent.readiness())
    except Exception:
        logger.exception(
            "SQLAgent init failed at startup — API starts; /ready may return 503"
        )

    import uvicorn

    uvicorn.run(
        "app.api:app",
        host=host,
        port=port,
        workers=1 if reload else max(1, workers),
        reload=reload,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
