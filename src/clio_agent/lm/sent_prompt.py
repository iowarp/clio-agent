"""What a session's model was last sent: its system text and tool schemas.

:class:`~clio_agent.lm.call_trace.LMCallTrace` remembers the last request of each GACT
session (:func:`remember`) and adds a small :func:`shape` of every request to its
``lm.call`` record, so prompt weight is measured from what was sent, not from a
reconstruction. ``GET /v1/sessions/{sid}/agent?exact=1`` serves :func:`exact_view`: the
text as the provider sees it (the ``claude_code`` engine renders the tools into the
system text) with sizes per tool and per plugin. Only the latest request per session is
kept, for the most recently active sessions.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from clio_agent import conf

__all__ = ["exact_view", "remember", "sent_prompt_sessions", "shape"]

_LOCK = threading.Lock()
_LAST: OrderedDict[str, dict[str, Any]] = OrderedDict()


def sent_prompt_sessions() -> int:
    """Sessions whose last sent request is kept (``lm.sent_prompt_sessions``, at least 1)."""
    value = conf.resolve(
        "lm.sent_prompt_sessions", env="CLIO_LM_SENT_PROMPT_SESSIONS", default=64, cast=conf.as_int
    )
    if value < 1:
        raise ValueError(f"lm.sent_prompt_sessions must be at least 1, not {value}")
    return value


def _tool_rows(request: Any) -> list[dict[str, Any]]:
    rows = []
    for tool in getattr(request, "tools", None) or ():
        name = getattr(tool, "name", None)
        if not name:
            continue
        rows.append(
            {
                "name": str(name),
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": getattr(tool, "parameters", None) or {},
            }
        )
    return rows


def _tool_chars(row: Mapping[str, Any]) -> int:
    return len(row["name"]) + len(row["description"]) + len(json.dumps(row["parameters"]))


def shape(request: Any) -> dict[str, Any]:
    """System length and per-tool schema length of one request, for its ``lm.call``."""
    system = getattr(request, "system", None) or ""
    return {
        "system_chars": len(system),
        "tools": [[row["name"], _tool_chars(row)] for row in _tool_rows(request)],
    }


def remember(session_id: str, model: str, request: Any) -> None:
    """Keep ``request`` as the latest one sent for ``session_id``."""
    if not session_id:
        return
    entry = {
        "model": model,
        "system": getattr(request, "system", None) or "",
        "tools": _tool_rows(request),
    }
    kept = sent_prompt_sessions()
    with _LOCK:
        _LAST[session_id] = entry
        _LAST.move_to_end(session_id)
        while len(_LAST) > kept:
            _LAST.popitem(last=False)


def _owner(name: str, owners: Mapping[str, str], servers: Mapping[str, str]) -> str:
    if name in owners:
        return owners[name]
    for server, plugin in servers.items():
        if name.startswith(f"{server}_"):
            return plugin
    return ""


def exact_view(
    session_id: str,
    *,
    tool_plugins: Mapping[str, str] | None = None,
    server_plugins: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The latest request sent for ``session_id`` with its sizes, or ``None``.

    ``tool_plugins`` and ``server_plugins`` attribute tools to the plugin that claims
    them (``server_plugins`` by the ``<server>_`` prefix); unclaimed tools count under
    ``""``. Sizes are characters; a token is roughly four.
    """
    with _LOCK:
        entry = _LAST.get(session_id)
    if entry is None:
        return None
    tools = entry["tools"]
    per_tool = {row["name"]: _tool_chars(row) for row in tools}
    per_plugin: dict[str, int] = {}
    for name, chars in per_tool.items():
        plugin = _owner(name, tool_plugins or {}, server_plugins or {})
        per_plugin[plugin] = per_plugin.get(plugin, 0) + chars
    rendered = None
    if str(entry["model"]).startswith("claude_code/"):
        from dspy.lm15 import FunctionTool  # noqa: PLC0415

        from clio_agent.lm.engines.text_tools import render_system  # noqa: PLC0415

        rendered = render_system(
            entry["system"],
            [FunctionTool(r["name"], r["description"], r["parameters"]) for r in tools],
        )
    return {
        "model": entry["model"],
        "system": entry["system"],
        "tools": tools,
        "rendered_system": rendered,
        "sizes": {
            "system_chars": len(entry["system"]),
            "tools_chars": sum(per_tool.values()),
            "rendered_chars": len(rendered) if rendered is not None else None,
            "per_tool": dict(sorted(per_tool.items(), key=lambda item: -item[1])),
            "per_plugin": dict(sorted(per_plugin.items(), key=lambda item: -item[1])),
        },
    }
