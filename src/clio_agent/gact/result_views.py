"""Views a tool result declares, shown as the result arrives (RV-D1).

A person judges a profile, a section, a field or a track by looking at it. A tool that
knows how its output is looked at names the view in its result: each entry of the
structured result's ``outputs`` may carry ``view``, one component of the conversation's
A2UI catalog with the output file as its data reference (clio-kit's ocean and weather
servers do). CLIO shows those views when the result arrives, in one surface per call,
the way an MCP App opens the view its tool names: the producer declared it, CLIO draws
it, nothing is decided from the content.

The agent is told in that call's result which surface holds them, so it can look at it
(``capture_a2ui_surface``), change or link it in place (``update_a2ui_components``) or
remove it. A view CLIO could not show (no conversation that draws views, an invalid
component, a file outside the workspace) is said in the result with the refusal's
reason, never dropped.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: Most views one result opens.
MAX_VIEWS = 6
#: Typed reasons for the trace.
SHOWN = "result_views_shown"
REFUSED = "result_views_refused"
FAILED = "result_views_failed"

_PENDING = threading.local()


def declared_views(structured: Any) -> list[dict[str, Any]]:
    """The views a structured tool result declares, in output order."""

    if not isinstance(structured, Mapping):
        return []
    outputs = structured.get("outputs")
    if not isinstance(outputs, list):
        return []
    views = [
        dict(output["view"])
        for output in outputs
        if isinstance(output, Mapping)
        and isinstance(output.get("view"), Mapping)
        and isinstance(output["view"].get("component"), str)
    ]
    return views[:MAX_VIEWS]


def _surface_components(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [f"view-{index + 1}" for index in range(len(views))]
    root = {"id": "root", "component": "Column", "children": ids}
    return [root, *({**view, "id": view_id} for view_id, view in zip(ids, views, strict=True))]


def _surface_id(tool: str) -> str:
    stem = "".join(ch if ch.isalnum() else "-" for ch in tool.split("_", 1)[-1]).strip("-")
    return f"{stem or 'result'}-{uuid.uuid4().hex[:8]}"


def _shown_note(result: Mapping[str, Any], views: list[dict[str, Any]]) -> str:
    named = ", ".join(
        f"{view['component']}" + (f" ({view['title']})" if view.get("title") else "")
        for view in views
    )
    return (
        f"CLIO showed this result to the person as views in surface {result['surface_id']} "
        f"(revision {result['revision']}): {named}. Look at it with capture_a2ui_surface "
        "and check it against your numbers; change or link it in place with "
        "update_a2ui_components (inspect_a2ui_surface gives its components), or delete it "
        "if it does not help."
    )


def _set_note(note: str) -> None:
    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    _PENDING.note = note
    _PENDING.call = peek_call_identity()


def show(app: Any, tool: str, structured: Any) -> None:
    """Show the views ``structured`` declares; the note for this call's model result."""

    _set_note("")
    views = declared_views(structured)
    if not views:
        return
    from clio_agent.gact.a2ui_producer.create import (  # noqa: PLC0415
        build_create_a2ui_surface_tool,
    )

    del app  # the create tool resolves the turn's app and session itself
    try:
        result = build_create_a2ui_surface_tool().func(
            surface_id=_surface_id(tool), components=_surface_components(views)
        )
    except Exception as exc:  # noqa: BLE001 - the tool itself succeeded; say so instead
        logger.exception("result views failed tool=%s reason=%s", tool, FAILED)
        _set_note(
            f"CLIO could not show the views this result declares ({type(exc).__name__}); "
            "build them with create_a2ui_surface if they help."
        )
        return
    if isinstance(result, Mapping) and result.get("rendered"):
        trace.event("TOOLS", "%s tool=%s surface=%s", SHOWN, tool, result.get("surface_id"))
        _set_note(_shown_note(result, views))
        return
    reason = (result or {}).get("error") or (result or {}).get("refusal") or "refused"
    detail = (result or {}).get("detail") or (result or {}).get("message") or ""
    trace.event("TOOLS", "%s tool=%s reason=%s", REFUSED, tool, reason)
    _set_note(
        f"CLIO could not show the views this result declares ({reason}"
        + (f": {detail}" if detail else "")
        + "); build them with create_a2ui_surface if they help."
    )


def merge_note(observation: Any) -> Any:
    """Append this call's views note to the model-visible result, once."""

    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    note = getattr(_PENDING, "note", "")
    if not note:
        return observation
    _PENDING.note = ""
    if getattr(_PENDING, "call", "") != peek_call_identity():
        return observation  # another call's note: never carried onto this one
    if not isinstance(observation, str):
        return observation
    return f"{observation}\n\n{note}"
