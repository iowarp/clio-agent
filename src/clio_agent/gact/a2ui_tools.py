"""Model-callable production of trusted A2UI 0.9.1 surfaces."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.a2ui import (
    A2UITranscriptFrozenError,
    A2UIValidationError,
    trusted_component_names,
)
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE, CLIO_A2UI_CATALOG_ID

# The surface registry rides back in the model lane on every production, so it
# is bounded: a long session's oldest surfaces are the ones least likely to be
# revised, so the newest ids survive the cut and the drop is stated, never
# silent.
MAX_REPORTED_SURFACE_IDS = 32


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
        the user's question more clearly than prose.
        ``surface_id`` selects the surface: to revise one that already exists,
        pass its EXACT id, listed in every result's ``session_surface_ids``;
        any other id creates a separate new surface. The result's ``created``
        reports which happened — ``true`` means a new surface was minted,
        ``false`` means the existing one was revised in place.
        ``components`` uses the A2UI
        0.9 component array: each item needs ``id`` and ``component``; containers
        reference child ids. Trusted component names: {trusted_component_names}.
        Bind values with
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
        part_id = f"live_a2ui_{uuid.uuid4().hex[:12]}"
        messages: list[dict[str, Any]] = []
        existing = app.state.a2ui_store.get(session_id, surface_id)
        if existing is None or existing.state == "deleted":
            messages.append(
                {
                    "version": A2UI_V091_WIRE,
                    "createSurface": {
                        "surfaceId": surface_id,
                        "catalogId": CLIO_A2UI_CATALOG_ID,
                    },
                }
            )
        messages.append(
            {
                "version": A2UI_V091_WIRE,
                "updateComponents": {
                    "surfaceId": surface_id,
                    "components": components,
                },
            }
        )
        if data_model is not None:
            messages.append(
                {
                    "version": A2UI_V091_WIRE,
                    "updateDataModel": {
                        "surfaceId": surface_id,
                        "path": "/",
                        "value": data_model,
                    },
                }
            )

        # Both the HTTP producer and this model tool cross the same atomic
        # validate-then-append service. Updates still write a projection-only
        # A2UI part, so replay remains complete without rendering duplicate cards.
        def persist_part(candidate: Part) -> bool:
            return _emit_surface_part(app, session_id, candidate)

        try:
            outcome = app.state.a2ui_store.apply_batch_outcome(
                session_id,
                messages,
                part_id=part_id,
                persist_part=persist_part,
            )
        except A2UITranscriptFrozenError:
            # The batch was valid but the turn's ledger is already settled, so
            # nothing was persisted or published: report the typed reason rather
            # than a validation message the model cannot act on.
            return {
                "rendered": False,
                "reason": "transcript_frozen",
                "session_id": session_id,
                "surface_id": surface_id,
            }
        surface = outcome.surfaces[-1]
        registry = outcome.session_surface_ids
        truncated = len(registry) > MAX_REPORTED_SURFACE_IDS
        result: dict[str, Any] = {
            "rendered": True,
            # Server truth from the fold, not from what this call intended: a
            # new surface and a revision otherwise land on the same revision
            # and state, so this is the only signal that separates them.
            "created": surface.id in outcome.created_surface_ids,
            "session_id": session_id,
            "surface_id": surface.id,
            "part_id": surface.part_id or part_id,
            "revision": surface.revision,
            "state": surface.state,
            "session_surface_ids": list(registry[-MAX_REPORTED_SURFACE_IDS:]),
        }
        if truncated:
            result["session_surface_ids_truncated"] = True
        return result

    create_a2ui_surface.__doc__ = (create_a2ui_surface.__doc__ or "").replace(
        "{trusted_component_names}", ", ".join(trusted_component_names())
    )
    return native_tool(
        create_a2ui_surface,
        name="create_a2ui_surface",
        desc=create_a2ui_surface.__doc__,
        title="Build Analysis View",
        args={
            "surface_id": {
                "type": "string",
                "description": (
                    "Stable surface id. Reuse an id from a previous result's "
                    "session_surface_ids to revise that surface; any other id "
                    "creates a new one."
                ),
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
