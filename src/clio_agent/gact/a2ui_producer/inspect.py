"""Read current surfaces for corrections and follow-ups about selected items."""

from __future__ import annotations

import json
from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool

MAX_DEFINITION_CHARS = 100_000


def build_inspect_a2ui_surface_tool() -> Any:
    """Build the read-only, session-scoped surface inspection tool."""

    def inspect_a2ui_surface(surface_id: str = "") -> dict[str, Any]:
        """Find an existing inline view or widget for a correction or follow-up.

        With no ``surface_id``, list live surfaces in this conversation with
        their ids, titles, types, and revisions. Pass one id to read its current
        components. Reuse that id with ``update_a2ui_components`` or
        ``create_a2ui_surface`` to correct the displayed surface in place.
        If an action supplies only a selected ID and a later question asks
        about fields from that item, inspect the existing surface to recover
        its displayed row details before answering.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        requested_id = surface_id.strip()
        if requested_id:
            surface = app.state.a2ui_store.get(session_id, requested_id)
            if surface is None or surface.state == "deleted":
                return refusal(
                    "a2ui_surface_not_found", detail=f"A2UI surface not found: {requested_id}"
                )
            components = _common.current_surface_components(surface)
            if len(json.dumps(components, ensure_ascii=False)) > MAX_DEFINITION_CHARS:
                return {
                    "surface_id": surface.id,
                    "revision": surface.revision,
                    "component_count": len(components),
                    "definition_too_large": True,
                    "detail": "This definition is too large to return through a tool result.",
                }
            return {
                "surface_id": surface.id,
                "revision": surface.revision,
                "catalog_id": surface.catalog_id,
                "components": components,
            }

        rows = [
            row
            for row in app.state.a2ui_store.list_wire(session_id)
            if row.get("state") != "deleted"
        ]
        summaries: list[dict[str, Any]] = []
        for row in rows[-32:]:
            components = _common.current_surface_components(
                app.state.a2ui_store.get(session_id, str(row["id"]))
            )
            named = next((item for item in components if item.get("title")), None)
            content = next((item for item in components if item.get("component") != "Column"), None)
            summaries.append(
                {
                    "surface_id": row["id"],
                    "revision": row["revision"],
                    "title": named.get("title") if named else "",
                    "component": content.get("component") if content else "",
                }
            )
        return {"surfaces": summaries, "total": len(rows), "truncated": len(rows) > 32}

    return native_tool(
        inspect_a2ui_surface,
        name="inspect_a2ui_surface",
        title="Inspect widget",
        domain="surfaces",
        presentation="text",
        read_only=True,
        desc=inspect_a2ui_surface.__doc__,
        args={
            "surface_id": {
                "type": "string",
                "description": "Existing id, or empty to list this session's surfaces.",
            }
        },
    )


__all__ = ["build_inspect_a2ui_surface_tool"]
