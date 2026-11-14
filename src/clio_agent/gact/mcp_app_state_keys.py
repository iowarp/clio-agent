"""What a view reports, as data the agent reads before it links the view (FP-D11).

A dashboard links a live view to its other components with ``clio.mcp-app.v1``
channels (MA21): ``state: [{key, path, tool?}]``, where ``key`` is a
``structuredContent`` key the view reports with ``ui/update-model-context`` and ``tool``
the server tool that moves the view to a value. Nothing told the agent which keys a
view reports, so it guessed them (first-run journey JR5). Two sources say so now:

* **Declared.** A view-opening tool's definition may carry, in its private ``_meta``::

      _meta["ai.iowarp.clio/state"] = [
          {"key": "time", "tool": "set_timestep", "description": "the shown time value"},
          {"key": "scene"},
      ]

  ``key`` matches ``^[A-Za-z0-9_.-]{1,64}$``; ``tool`` (optional) is the server's own
  name of the tool that sets it; ``description`` (optional) is at most 200 characters.
  A malformed entry is ignored with the typed log reason ``state_declaration_invalid``.
* **Reported.** The ``structuredContent`` keys a live view of the same server and tool
  last reported in this session (``session.metadata["mcp_app_context"]``).

The agent reads them in the live view's context block (``clio.reminder.mcp_app_context``)
and in the producer's refusals: ``a2ui_mcp_app_tool_not_found`` lists each view tool's
keys, and ``a2ui_mcp_app_state_key_unknown`` refuses a channel key that a declaring
server does not report. A server that declares nothing is never refused on its keys:
what a view reported once is a hint, not the whole list.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The private tool-definition ``_meta`` key a server declares its view's state under.
STATE_META_KEY = "ai.iowarp.clio/state"

_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_DESCRIPTION = 200
_MAX_KEYS = 64


@dataclass(frozen=True)
class StateKey:
    """One key a view reports: declared by its server, or seen in its last report."""

    key: str
    tool: str = ""
    description: str = ""
    declared: bool = True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "declared": self.declared}
        if self.tool:
            out["tool"] = self.tool
        if self.description:
            out["description"] = self.description
        return out

    def words(self) -> str:
        """``time (set with set_timestep)``, for model text."""

        return f"{self.key} (set with {self.tool})" if self.tool else self.key


def _tool_meta(tool: Any) -> Mapping[str, Any]:
    from clio_agent.tools.tool_path_contract import _tool_meta as read  # noqa: PLC0415

    return read(tool)


def _invalid(tool_name: str, detail: str) -> None:
    logger.warning(
        "view state declaration ignored reason=state_declaration_invalid tool=%s detail=%s",
        tool_name,
        detail,
    )


def declared_state_keys(tool: Any, tool_name: str = "") -> list[StateKey]:
    """The keys ``tool``'s definition declares its view reports (empty when none)."""

    raw = _tool_meta(tool).get(STATE_META_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        _invalid(tool_name, "declaration_not_a_list")
        return []
    out: list[StateKey] = []
    seen: set[str] = set()
    for entry in raw[:_MAX_KEYS]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("key"), str):
            _invalid(tool_name, "entry_without_key")
            continue
        key, setter, description = (
            entry["key"],
            entry.get("tool", ""),
            entry.get("description", ""),
        )
        if not _KEY.match(key) or key in seen:
            _invalid(tool_name, f"key_invalid:{key[:64]!r}")
            continue
        if setter and (not isinstance(setter, str) or not _TOOL.match(setter)):
            _invalid(tool_name, f"tool_invalid:{key}")
            setter = ""
        if not isinstance(description, str) or "\n" in description:
            description = ""
        seen.add(key)
        out.append(StateKey(key, setter, description[:_MAX_DESCRIPTION]))
    return out


def reported_state_keys(app: Any, sid: str, server: str, tool: str) -> list[str]:
    """The ``structuredContent`` keys this session's live views of ``server``/``tool``
    last reported, in first-seen order."""

    from clio_agent.gact.mcp_app_context import _entries  # noqa: PLC0415

    keys: list[str] = []
    for entry in _entries(app, sid).values():
        if entry.get("server") != server or _bare(entry.get("tool"), server) != tool:
            continue
        context = entry.get("context")
        structured = context.get("structuredContent") if isinstance(context, Mapping) else None
        if isinstance(structured, Mapping):
            keys.extend(k for k in structured if isinstance(k, str) and k not in keys)
    return [key for key in keys if _KEY.match(key)][:_MAX_KEYS]


def _bare(tool: Any, server: str) -> str:
    name = str(tool or "")
    prefix = f"{server}_"
    return name[len(prefix) :] if server and name.startswith(prefix) else name


def state_keys(
    app: Any, sid: str | None, server: str, tool: str, definition: Any
) -> list[StateKey]:
    """Declared keys first, then keys only a live view reported."""

    declared = declared_state_keys(definition, f"{server}_{tool}")
    known = {item.key for item in declared}
    reported = reported_state_keys(app, sid, server, tool) if sid else []
    return declared + [StateKey(k, declared=False) for k in reported if k not in known]


def _record_definitions(app: Any, record: Any) -> Mapping[str, Any]:
    """The tool definitions of the fleet whose server opened ``record``.

    The context block is built at the start of a turn, before the session's workspace
    is bound, where the agent's active executor is its default one; the record keeps a
    weak reference to the fleet that opened it (``mcp_app_fleet.admitted``).
    """

    ref = getattr(record, "fleet", None)
    fleet = ref() if callable(ref) else None
    listing = getattr(fleet, "get_all_tool_definitions", None)
    if callable(listing):
        return listing() or {}
    from clio_agent.gact.a2ui_producer._mcp_app_reference import (  # noqa: PLC0415
        _tool_definitions,
    )

    return _tool_definitions(app) or {}


def record_state_words(app: Any, sid: str, record: Any) -> str:
    """The keys an open view reports, for its context block (``""`` when none are known)."""

    server = record.source_namespace or ""
    tool = _bare(record.tool_name, server)
    definition = _record_definitions(app, record).get(f"{server}_{tool}")
    return describe(state_keys(app, sid, server, tool, definition))


def describe(keys: list[StateKey]) -> str:
    """``time (set with set_timestep), scene``, or ``""``."""

    return ", ".join(item.words() for item in keys)


__all__ = [
    "STATE_META_KEY",
    "StateKey",
    "declared_state_keys",
    "describe",
    "record_state_words",
    "reported_state_keys",
    "state_keys",
]
