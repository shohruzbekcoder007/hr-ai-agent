"""
Hermes plugin: hr-employee (SQL Agent)

Registers the SQL toolset (toolset name: ``sql``) with Hermes via
``ctx.register_tool(...)``. Does not modify Hermes core.

Discovery:
  * User plugins: $HERMES_HOME/plugins/hr-employee/
  * Enabled via config.yaml → plugins.enabled: [hr-employee]
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hr_agent.plugin")


def _ensure_app_on_path() -> None:
    """Allow importing hr_tools when the plugin is loaded from HERMES_HOME."""
    candidates = [
        Path(os.getenv("HR_APP_ROOT", "/app")),
        Path(__file__).resolve().parents[2],  # project root when in-repo
    ]
    for root in candidates:
        if (root / "hr_tools" / "sql_tool.py").is_file() or (
            root / "hr_tools"
        ).is_dir():
            root_s = str(root)
            if root_s not in sys.path:
                sys.path.insert(0, root_s)
            return


def _load_tool_module() -> Any:
    _ensure_app_on_path()
    try:
        import hr_tools.sql_tool as mod  # type: ignore

        return mod
    except ImportError:
        root = Path(os.getenv("HR_APP_ROOT", "/app"))
        tool_path = root / "hr_tools" / "sql_tool.py"
        if not tool_path.is_file():
            tool_path = (
                Path(__file__).resolve().parents[2] / "hr_tools" / "sql_tool.py"
            )
        import importlib.util
        import types

        # Ensure package shell
        if "hr_tools" not in sys.modules:
            sys.modules["hr_tools"] = types.ModuleType("hr_tools")

        # Load db_service first
        service_path = tool_path.parent / "db_service.py"
        svc_spec = importlib.util.spec_from_file_location(
            "hr_tools.db_service", service_path
        )
        if svc_spec and svc_spec.loader:
            svc_mod = importlib.util.module_from_spec(svc_spec)
            sys.modules["hr_tools.db_service"] = svc_mod
            svc_spec.loader.exec_module(svc_mod)
            sys.modules["hr_tools"].db_service = svc_mod  # type: ignore[attr-defined]

        spec = importlib.util.spec_from_file_location(
            "hr_tools.sql_tool", tool_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load SQL tools from {tool_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hr_tools.sql_tool"] = mod
        spec.loader.exec_module(mod)
        return mod


def register(ctx: Any) -> None:
    """Hermes plugin entrypoint — called by PluginManager."""
    mod = _load_tool_module()

    try:
        from hr_tools.db_service import get_database_service

        readiness = get_database_service().readiness()
        logger.info("hr-employee plugin: database readiness=%s", readiness)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hr-employee plugin: could not probe database: %s", exc)

    schemas = {s["name"]: s for s in mod.TOOL_SCHEMAS}
    handlers = mod.get_tool_handlers()
    toolset = getattr(mod, "TOOLSET_NAME", "sql")

    def _bind(tool_name: str, h: Any):
        def _handle(params: dict, **kwargs: Any) -> str:
            del kwargs
            try:
                return h(params or {})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tool %s failed", tool_name)
                return json.dumps({"success": False, "error": str(exc)})

        return _handle

    for name, handler in handlers.items():
        schema = schemas.get(name)
        if not schema:
            continue

        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=_bind(name, handler),
            description=schema.get("description", name),
        )
        logger.info("Registered Hermes tool %s (toolset=%s)", name, toolset)

    def on_tool_call(tool_name: str, params: Any, result: Any) -> None:
        if tool_name in handlers:
            logger.debug(
                "sql tool called: %s params_keys=%s",
                tool_name,
                list((params or {}).keys()),
            )

    try:
        ctx.register_hook("post_tool_call", on_tool_call)
    except Exception:
        pass

    logger.info("hr-employee (SQL) plugin registered (%d tools)", len(handlers))
