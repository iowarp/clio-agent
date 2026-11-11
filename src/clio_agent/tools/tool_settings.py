"""Per-tool I/O (``callers``) and default permission: vocabulary and configured values.

Every tool has two settings (tool-calling plan, "Design: tool I/O and default
permissions"; ledger TD52-TD55):

- ``callers``: ``direct`` (the model calls it, scripts cannot), ``code`` (only
  ``run_code`` scripts call it; its schema stays out of the prompt) or ``both``
  (the default).
- a default ``permission``: ``allow``, ``ask`` or ``deny``. It is the lowest policy
  layer of the permission gate: the person's rules override it and it overrides the
  approval mode (:mod:`clio_agent.gact.runtime.tool_permission_layers`).

A plugin declares both (``tool(callers=, permission=)``, ``native_tool(name, callers=,
permission=)``, ``plugin.json`` server and tool entries); a CLIO built-in may declare
its own ``callers`` where it is constructed. The deployment's values resolve through
:mod:`clio_agent.conf` as ``tools.callers`` and ``tools.permissions`` (mappings from
tool name; ``CLIO_TOOLS_CALLERS`` / ``CLIO_TOOLS_PERMISSIONS`` as JSON) and win over
every declaration. The words themselves are protocol vocabulary and stay in code.

This module is a leaf: it imports nothing from ``gact``, so the SDK, the plugin
loader and the gate can all share it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent import conf

__all__ = [
    "CALLERS",
    "CALLERS_ATTR",
    "CALLERS_BOTH",
    "CALLERS_CODE",
    "CALLERS_DIRECT",
    "PERMISSIONS",
    "SELF_GATED_ATTR",
    "TOOL_SETTINGS_VALUE_INVALID",
    "annotation_permission",
    "check_callers",
    "check_permission",
    "configured_callers",
    "configured_permissions",
    "self_gated",
]

logger = logging.getLogger(__name__)

CALLERS_DIRECT = "direct"
CALLERS_CODE = "code"
CALLERS_BOTH = "both"
#: Who may call a tool.
CALLERS = (CALLERS_DIRECT, CALLERS_CODE, CALLERS_BOTH)
#: A tool's default permission.
PERMISSIONS = ("allow", "ask", "deny")
#: Attribute a CLIO built-in's callable carries when it declares its own ``callers``.
CALLERS_ATTR = "_clio_callers"
#: Attribute a CLIO built-in's callable carries when the tool asks the permission gate
#: itself before it acts (``run_code``, ``make_plugin``, connecting sources), so the
#: tools page can say what the approval mode does with it.
SELF_GATED_ATTR = "_clio_self_gated"
#: Typed reason: a configured ``tools.callers`` / ``tools.permissions`` entry was not
#: understood and is ignored (logged with the key and the value).
TOOL_SETTINGS_VALUE_INVALID = "tool_settings_value_invalid"


def check_callers(value: Any, where: str) -> str | None:
    """``value`` if it is a known caller setting (or ``None``); else ``ValueError``."""
    if value is None or value in CALLERS:
        return value
    raise ValueError(f"{where}: callers must be one of {', '.join(CALLERS)}, not {value!r}")


def check_permission(value: Any, where: str) -> str | None:
    """``value`` if it is a known default permission (or ``None``); else ``ValueError``."""
    if value is None or value in PERMISSIONS:
        return value
    raise ValueError(f"{where}: permission must be one of {', '.join(PERMISSIONS)}, not {value!r}")


def _valid(raw: Mapping[str, Any], allowed: tuple[str, ...], key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(name, str) and name and value in allowed:
            out[name] = str(value)
        else:
            logger.warning(
                "%s key=%s tool=%r value=%r expected=%s",
                TOOL_SETTINGS_VALUE_INVALID,
                key,
                name,
                value,
                "|".join(allowed),
            )
    return out


def _read(read: Callable[[], Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    try:
        return read()
    except ValueError as exc:
        logger.warning("%s key=%s error=%s", TOOL_SETTINGS_VALUE_INVALID, key, exc)
        return {}


def configured_callers() -> dict[str, str]:
    """``tools.callers``: tool name to ``direct``, ``code`` or ``both``."""
    raw = _read(
        lambda: conf.resolve(
            "tools.callers",
            env="CLIO_TOOLS_CALLERS",
            default={},
            cast=conf.as_mapping,
        ),
        "tools.callers",
    )
    return _valid(raw, CALLERS, "tools.callers")


def configured_permissions() -> dict[str, str]:
    """``tools.permissions``: the deployment's default permission per tool name."""
    raw = _read(
        lambda: conf.resolve(
            "tools.permissions",
            env="CLIO_TOOLS_PERMISSIONS",
            default={},
            cast=conf.as_mapping,
        ),
        "tools.permissions",
    )
    return _valid(raw, PERMISSIONS, "tools.permissions")


def annotation_permission(read_only: bool, destructive: bool) -> str:
    """The permission a tool's annotations imply when nothing is declared.

    Read-only tools run (``allow``), destructive tools ``ask``; ``""`` leaves the call to
    the approval mode. This is what the gate already does with annotations, so it is
    shown as the default but adds no rule of its own.
    """
    if read_only and not destructive:
        return "allow"
    if destructive:
        return "ask"
    return ""


def self_gated(tool: Any) -> Any:
    """Mark a built-in tool that asks the permission gate itself; returns it."""
    setattr(getattr(tool, "func", tool), SELF_GATED_ATTR, True)
    return tool
