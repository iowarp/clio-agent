"""Model-callable production of trusted A2UI 0.9.1 surfaces."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.a2ui import A2UIValidationError, validate_server_message
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID


def _emit_surface_part(app: Any, session_id: str, part: Part) -> bool:
    """Append a typed A2UI reference in live transcript order."""

    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
        _mirror_transcript_state,
        _session_turn_transcript,
    )

    transcript = _session_turn_transcript(app, session_id)
    if transcript is None:
        _append_live_assistant_part(app, session_id, part)
        return True
    appended = transcript.append_part(part)
    if appended is None:
        return False
    _mirror_transcript_state(app, session_id, transcript)
    return True


def build_create_a2ui_surface_tool() -> Any:
    """Build the root-agent tool that produces or updates a trusted surface."""

    def create_a2ui_surface(
        surface_id: str,
        components: list[dict[str, Any]],
        data_model: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create or update an interactive analysis view in this conversation.

        Use this for rendered Mermaid diagrams, highlighted code, compact plots,
        data tables, structured status, workflows, artifacts, diffs, approvals,
        and forms when those representations answer
        the user's question more clearly than prose. ``components`` uses the A2UI
        0.9 component array: each item needs ``id`` and ``component``; containers
        reference child ids. Trusted names include Text, Icon, Image, Row, Column,
        Grid, List, Frame, Tabs, Modal, Divider, Button, Checkbox, TextField,
        ChoicePicker, Slider, clio.status.v1, clio.metric.v1, clio.progress.v1,
        clio.callout.v1, clio.data-table.v1, clio.time-series.v1,
        clio.mermaid.v1, clio.map.v1, clio.workflow.v1, clio.artifact.v1,
        clio.code.v1, clio.diff.v1,
        clio.action-card.v1, and clio.approval.v1. Bind values with
        ``{"path": "/field"}`` and supply those values in ``data_model``. The
        shape is flat and must contain exactly one top-level component whose id is
        the literal ``"root"`` because the official renderer mounts that id. For
        example ``{"id": "root", "component": "Column",
        "children": ["status"]}`` followed by ``{"id": "status",
        "component": "clio.status.v1", "label": "Run", "state":
        {"path": "/state"}}``. ``component`` is always a string, never a nested
        object and never ``{"type": ...}``. Tabs use the official 0.9 shape
        ``{"id": "views", "component": "Tabs", "tabs": [{"title":
        "Plot", "child": "plot-view"}, {"title": "Data", "child":
        "table-view"}]}``; Tabs never use a ``children`` property. Each tab child
        is the id of a separately declared component. A Button references exactly one
        child with ``"child": "label-id"`` (not ``children``) and a server
        action is nested under the Button's ``action`` property. For example,
        the complete Button component is ``{"id": "submit", "component":
        "Button", "child": "label-id", "action": {"event": {"name":
        "form.submit", "context": {"selection": {"path":
        "/selection"}}}}}``. Do not nest a second ``action`` object. A
        ``clio.status.v1`` accepts ``label``, ``state``, ``detail``, and
        ``elapsedMs``; use ``detail`` rather than a ``message`` property.
        Registered event context requirements are: ``agent.submit`` needs
        ``text`` or ``prompt``; ``approval.respond`` needs ``permission_id``
        and ``action``; ``run.retry`` needs ``message_id``; ``run.cancel``
        needs no context; and ``form.submit`` accepts the submitted form fields.
        For small plots, ``clio.time-series.v1.series`` is an array of row
        objects. For a registered CSV artifact, omit ``series`` and use
        ``dataUri: artifact://<artifact-id>`` instead; the renderer requests an
        integrity-checked bounded preview rather than loading the entire file.
        Exactly one of ``series`` or ``dataUri`` is allowed. ``xKey`` names the
        x column and ``yKeys`` names one to five numeric columns. The rendered
        chart provides hover values, series visibility, zoom, and pan.
        For tables, ``columns`` may be string field names or objects like
        ``{"key": "displacement_mm", "label": "Displacement (mm)"}``, and
        ``rows`` is an array of objects keyed by those fields. A
        ``clio.data-table.v1`` does not accept ``title``; compose a separate Text
        component above it inside a Column when the table needs a visible title.
        For maps, ``clio.map.v1.points`` is a bounded array of objects with
        ``id``, ``label``, numeric ``latitude`` and ``longitude``, plus optional
        ``detail`` and ``category``. The renderer owns its trusted basemap; never
        provide tile, style, image, script, or geocoding URLs.
        A ``clio.callout.v1`` requires ``title``, ``body``, and ``severity``;
        it never accepts ``text`` or ``level``. An ``Image`` uses ``url`` (not
        ``src``), and that URL must use ``https:``, ``artifact:``, or
        ``resource:``. A ``clio.artifact.v1`` requires ``name``, ``uri``, and
        ``mediaType``. Use the exact URI returned by artifact registration, or
        an ``artifact://<artifact-id>`` URI when the registration result only
        supplies an id; never pass ``artifact_id``, ``kind``, ``path``, or a
        bare filesystem path as component properties.
        A ``clio.metric.v1`` represents exactly one metric and requires
        ``label`` and ``value``. To show several metrics, declare one metric
        component per value and reference their ids from a Row or Grid. It does
        not accept a ``metrics`` aggregate property.
        For Mermaid, pass declarative source in ``source``; HTML, init directives,
        click handlers, and links are rejected. For code, pass ``code``,
        ``language``, and an optional ``title``. These components render visually;
        do not wrap their payloads in Text or JSON blocks.
        Accessibility is always an object, never a string: use
        ``"accessibility": {"label": "Readable label", "description":
        "Optional extra context"}``. Either value may instead be a data binding
        such as ``{"path": "/accessibleLabel"}``.
        Only registered actions are accepted; never send HTML, CSS, scripts,
        imports, commands, executable URLs, or event handlers.
        """

        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            return {
                "error": "a2ui_session_unavailable",
                "message": "A2UI production requires an active GACT session.",
            }
        if app.state.sessions.get(session_id) is None:
            return {
                "error": "a2ui_session_not_found",
                "message": f"Session not found: {session_id}",
            }
        surface_id = surface_id.strip()
        root_components = [component for component in components if component.get("id") == "root"]
        if len(root_components) != 1:
            raise A2UIValidationError(
                'A2UI surface components must contain exactly one id="root" component'
            )
        part = Part(
            id=f"live_a2ui_{uuid.uuid4().hex[:12]}",
            type="a2ui",
            surface_id=surface_id,
        )
        messages: list[dict[str, Any]] = []
        existing = app.state.a2ui_store.get(session_id, surface_id)
        has_transcript_reference = bool(
            existing is not None and existing.state != "deleted" and existing.part_id
        )
        if existing is None or existing.state == "deleted":
            messages.append(
                {
                    "version": "v0.9.1",
                    "createSurface": {
                        "surfaceId": surface_id,
                        "catalogId": CLIO_A2UI_CATALOG_ID,
                    },
                }
            )
        messages.append(
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": surface_id,
                    "components": components,
                },
            }
        )
        if data_model is not None:
            messages.append(
                {
                    "version": "v0.9.1",
                    "updateDataModel": {
                        "surfaceId": surface_id,
                        "path": "/",
                        "value": data_model,
                    },
                }
            )
        # Validate the entire batch before the first persistent mutation. A bad
        # component must never leave behind a half-created, permanently loading
        # surface. Raising also makes native tool telemetry truthfully failed.
        for message in messages:
            validate_server_message(message)
        surface = None
        for message in messages:
            surface = app.state.a2ui_store.apply(
                session_id,
                message,
                part_id=part.id,
            )
        assert surface is not None
        if has_transcript_reference:
            # The surface event updates every consumer of this stable id. A new
            # transcript reference would render the same evolving surface again
            # after every revision, producing duplicate cards instead of one
            # in-place interactive view.
            emitted = True
            part.id = surface.part_id
        else:
            emitted = _emit_surface_part(app, session_id, part)
        return {
            "rendered": emitted,
            "session_id": session_id,
            "surface_id": surface.id,
            "part_id": surface.part_id or part.id,
            "revision": surface.revision,
            "state": surface.state,
            **({} if emitted else {"reason": "transcript_frozen"}),
        }

    return native_tool(
        create_a2ui_surface,
        name="create_a2ui_surface",
        desc=create_a2ui_surface.__doc__,
        title="Build Analysis View",
        args={
            "surface_id": {
                "type": "string",
                "description": "Stable surface id, reused to update the same surface.",
            },
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": (
                        "A flat component with string id and component fields. "
                        "If present, accessibility is an object with label and/or "
                        "description; it is never a string."
                    ),
                },
                "description": "Official A2UI 0.9 component definitions in root-first order.",
            },
            "data_model": {
                "type": "object",
                "description": "Optional root data model for component path bindings.",
            },
        },
    )


__all__ = ["build_create_a2ui_surface_tool"]
