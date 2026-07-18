"""
Hermes plugin: hr-employee

Registers the HR toolset (toolset name: ``hr``) with Hermes via
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
        # Prefer hr_tools/ package dir (must not be named tools/ — that would
        # shadow Hermes's top-level tools package when root is on sys.path).
        if (root / "hr_tools" / "employee_service.py").is_file() or (
            root / "hr_tools"
        ).is_dir():
            root_s = str(root)
            if root_s not in sys.path:
                sys.path.insert(0, root_s)
            # Prefer installed package; fall back to path-based import
            return


def _load_tool_module() -> Any:
    _ensure_app_on_path()
    try:
        import hr_tools.employee_tool as mod  # type: ignore

        return mod
    except ImportError:
        # Fallback: load hr_tools/employee_tool.py by path
        root = Path(os.getenv("HR_APP_ROOT", "/app"))
        tool_path = root / "hr_tools" / "employee_tool.py"
        if not tool_path.is_file():
            tool_path = (
                Path(__file__).resolve().parents[2] / "hr_tools" / "employee_tool.py"
            )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hr_employee_tool_fallback", tool_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load employee tools from {tool_path}")
        mod = importlib.util.module_from_spec(spec)
        # Ensure sibling employee_service is importable under a synthetic package
        sys.modules[spec.name] = mod
        # Preload service module under expected name used by employee_tool
        service_path = tool_path.parent / "employee_service.py"
        svc_spec = importlib.util.spec_from_file_location(
            "hr_tools.employee_service", service_path
        )
        if svc_spec and svc_spec.loader:
            import types

            svc_mod = importlib.util.module_from_spec(svc_spec)
            pkg = types.ModuleType("hr_tools")
            sys.modules["hr_tools"] = pkg
            sys.modules["hr_tools.employee_service"] = svc_mod
            svc_spec.loader.exec_module(svc_mod)
            pkg.employee_service = svc_mod  # type: ignore[attr-defined]
        # Patch import inside tool file: it uses hr_tools.employee_service
        spec.loader.exec_module(mod)
        return mod


def register(ctx: Any) -> None:
    """Hermes plugin entrypoint — called by PluginManager."""
    mod = _load_tool_module()

    # Ensure employees.json is loaded at plugin registration time
    try:
        from hr_tools.employee_service import get_employee_service

        readiness = get_employee_service().readiness()
        logger.info("hr-employee plugin: employee readiness=%s", readiness)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hr-employee plugin: could not preload employees: %s", exc)

    schemas = {s["name"]: s for s in mod.TOOL_SCHEMAS}
    handlers = mod.get_tool_handlers()
    toolset = getattr(mod, "TOOLSET_NAME", "hr")

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

    # Optional lifecycle hook for observability
    def on_tool_call(tool_name: str, params: Any, result: Any) -> None:
        if tool_name in handlers:
            logger.debug("hr-employee tool called: %s params_keys=%s", tool_name, list((params or {}).keys()))

    try:
        ctx.register_hook("post_tool_call", on_tool_call)
    except Exception:
        # Older Hermes builds may not expose hooks the same way
        pass

    logger.info("hr-employee plugin registered (%d tools)", len(handlers))
