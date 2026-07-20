"""
Process entrypoint for the HR AI Agent container.

Usage:
  python -m app.main
  hr-agent   # console script after pip install
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    """Ensure project root is importable when run as a script."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    _bootstrap_paths()

    # Load .env if present (local dev); Docker Compose injects env directly
    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.is_file():
            load_dotenv(env_file)
    except Exception:
        pass

    from app.logging_setup import setup_logging

    setup_logging()
    logger = logging.getLogger("hr_agent")

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    workers = int(os.getenv("API_WORKERS", "1"))
    reload = os.getenv("API_RELOAD", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info("=" * 60)
    logger.info("SQL Agent — production server starting")
    logger.info("Host=%s Port=%s Workers=%s Reload=%s", host, port, workers, reload)
    logger.info("HERMES_HOME=%s", os.getenv("HERMES_HOME", ""))
    logger.info(
        "DATABASE_URL configured=%s",
        bool(
            os.getenv("DATABASE_URL")
            or os.getenv("HR_DATABASE_URL")
            or os.getenv("POSTGRES_URL")
        ),
    )
    logger.info("HR_MODEL=%s", os.getenv("HR_MODEL", ""))
    logger.info("HR_ENABLED_TOOLSETS=%s", os.getenv("HR_ENABLED_TOOLSETS", "sql"))
    logger.info("=" * 60)

    # Eager init so healthcheck becomes ready only after load
    from agents.hr_agent import get_hr_agent

    try:
        agent = get_hr_agent()
        logger.info("Agent readiness: %s", agent.readiness())
    except Exception:
        logger.exception(
            "Failed to initialize HR Agent at startup — "
            "API will start but /ready will return 503 until fixed"
        )

    import uvicorn

    # workers>1 with reload is invalid; prefer single worker for agent state
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
