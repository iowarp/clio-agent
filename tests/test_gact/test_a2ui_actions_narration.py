"""S5b: sidecar-declared event narration (clio-agent#1363 live-gate finding).

On Codex, a resumed turn read the S5 legacy ``A2UI event: <name>`` + JSON
narration as a REPORT rather than a REQUEST and never staged the selected
stations. clio-schemas 0.3.2 lets a pack author declare an event's MEANING
via ``CatalogSidecar.events[<name>].narration`` (rendered against the
resolved context by ``clio_schemas.a2ui.sidecar.render_narration``); these
are the failing-first reproducers for that declared-vs-fallback contract:
the declared template becomes the narration (still carrying the canonical
context JSON so a text-only provider gets the full structured object), an
undeclared event keeps the S5 legacy form and records the typed
``a2ui_event_narration_undeclared`` reason ONCE per (session, event name),
an over-long rendered narration is bounded with the existing truncation
marker, and the steer lane carries the identical text the start lane would.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from clio_agent.gact.a2ui_actions.narration import MAX_NARRATION_BYTES, narration_for
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.protocol.constants import A2UI_V091

from .test_a2ui_v3 import HEADERS, _create_message, _session_client

WORKSPACE_CATALOG_ID = workspace_catalog_id()

#: The exact event name from the live-gate finding (clio-agent#1363).
EVENT_NAME = "earthscope.stations.selected"
NARRATION_TEMPLATE = "The user selected {stationIds} for {searchId}: analyse them."


def _route_for(name: str, narration: str, *, context_schema: dict[str, Any] | None = None) -> Any:
    """Build ONE real, validated sidecar route via the public ``CatalogSidecar``.

    Exercises the exact same pydantic construction/validation
    (``_EventRoute``'s placeholder-vs-``context_schema`` check) a real pack's
    ``catalog.clio.json`` goes through -- never a hand-built stand-in.
    """

    from clio_schemas.a2ui.sidecar import CatalogSidecar

    payload: dict[str, Any] = {"destination": "agent", "narration": narration}
    if context_schema is not None:
        payload["context_schema"] = context_schema
    sidecar = CatalogSidecar.model_validate(
        {
            "catalogId": "test://a2ui-narration",
            "protocolVersion": "0.9.1",
            "trust": {"source": "pack"},
            "events": {name: payload},
        }
    )
    return sidecar.events[name]


def _install_narration_event(
    app: Any,
    monkeypatch: MonkeyPatch,
    *,
    name: str = EVENT_NAME,
    narration: str = NARRATION_TEMPLATE,
) -> None:
    """Patch the workspace catalog's sidecar to declare ``name``'s narration.

    A SYNTHETIC sidecar (mirrors ``test_a2ui_actions_review.py``'s
    ``_install_context_schema_event`` -- never an installed pack fixture, so
    this carries no risk of leaking a fixture pack into the developer's real
    Agent Blueprint registry).
    """

    from clio_schemas.a2ui.sidecar import CatalogSidecar

    registry = app.state.a2ui_catalogs
    original_get = registry.get

    def patched_get(catalog_id: str, protocol_version: str = A2UI_V091) -> Any:
        entry = original_get(catalog_id, protocol_version)
        if entry is None or catalog_id != WORKSPACE_CATALOG_ID:
            return entry
        events = {
            existing_name: route.model_dump(exclude_none=True)
            for existing_name, route in entry.sidecar.events.items()
        }
        events[name] = {
            "destination": "agent",
            "narration": narration,
            "context_schema": {
                "type": "object",
                "required": ["searchId", "stationIds"],
                "properties": {
                    "searchId": {"type": "string"},
                    "stationIds": {"type": "array", "minItems": 1},
                },
            },
        }
        new_sidecar = CatalogSidecar.model_validate(
            {**entry.sidecar.model_dump(by_alias=True, exclude_none=True), "events": events}
        )
        return replace(entry, sidecar=new_sidecar)

    monkeypatch.setattr(registry, "get", patched_get)


def _stub_spawn(app: Any, monkeypatch: MonkeyPatch) -> list[Any]:
    """Prevent a real turn from executing; return the list of spawned coros."""

    spawned: list[Any] = []

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(app.state.turn_runner, "spawn", _spawn)
    return spawned


def _event(name: str, surface_id: str, context: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "sourceComponentId": "source",
            "timestamp": timestamp,
            "context": context,
        },
    }


def _text(message: Any) -> str:
    return "".join(part.text for part in message.parts if part.type == "text")


def _canonical(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# Declared narration: the pack author's meaning leads, context JSON follows   #
# --------------------------------------------------------------------------- #


def test_declared_narration_renders_template_and_still_carries_context_json(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_narration_event(app, monkeypatch)
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    context = {"stationIds": ["MTA1", "PKRD"], "searchId": "earthscope-x"}
    action = _event(EVENT_NAME, "surface_1", context, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "start"
    message = next(m for m in reversed(app.state.messages[sid]) if m.role == "user")
    text = _text(message)
    expected_prefix = 'The user selected ["MTA1","PKRD"] for earthscope-x: analyse them.'
    assert text.startswith(expected_prefix)
    expected_text = f"{expected_prefix}\n\nStructured context:\n{_canonical(context)}"
    assert text == expected_text
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["narration"] == text
    # A declared narration never trips the undeclared reason.
    reasons = [
        row
        for row in app.state.a2ui_catalogs.session_reasons(sid)
        if row["reason"] == "a2ui_event_narration_undeclared"
    ]
    assert reasons == []


def test_steer_lane_carries_the_identical_declared_narration_text(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_narration_event(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )

    class _LiveTurn:
        def done(self) -> bool:
            return False

    app.state.in_flight_turns[sid] = _LiveTurn()
    context = {"stationIds": ["MTA1", "PKRD"], "searchId": "earthscope-x"}
    action = _event(EVENT_NAME, "surface_1", context, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "steer"
    inbox = app.state.loop_inboxes[sid]
    [queued] = inbox.snapshot()
    expected_prefix = 'The user selected ["MTA1","PKRD"] for earthscope-x: analyse them.'
    expected_text = f"{expected_prefix}\n\nStructured context:\n{_canonical(context)}"
    assert queued.text == expected_text
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["narration"] == expected_text


# --------------------------------------------------------------------------- #
# Undeclared event: legacy form, typed reason recorded ONCE per event name    #
# --------------------------------------------------------------------------- #


def test_undeclared_event_delivers_legacy_form_and_records_reason_once(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    context = {"stationIds": ["SGPS"]}
    first_action = _event(EVENT_NAME, "surface_1", context, "2026-09-17T00:00:00Z")
    second_action = _event(EVENT_NAME, "surface_1", context, "2026-09-17T00:00:01Z")

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": first_action}
    )
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": second_action}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["action_id"] != second.json()["action_id"]
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert len(surface.actions) == 2
    for record in surface.actions:
        expected_text = f"A2UI event: {EVENT_NAME}\n\nStructured context:\n{_canonical(context)}"
        assert record["narration"] == expected_text

    reasons = [
        row
        for row in app.state.a2ui_catalogs.session_reasons(sid)
        if row["reason"] == "a2ui_event_narration_undeclared"
    ]
    assert len(reasons) == 1
    assert reasons[0]["action"] == EVENT_NAME


# --------------------------------------------------------------------------- #
# Bounding: an over-long rendered narration truncates, the JSON still travels #
# --------------------------------------------------------------------------- #


def test_overlong_rendered_narration_is_bounded_with_truncation_marker() -> None:
    route = _route_for(EVENT_NAME, NARRATION_TEMPLATE)
    huge_station_ids = [f"STATION_{i:05d}" for i in range(400)]
    context = {"stationIds": huge_station_ids, "searchId": "earthscope-x"}

    text = narration_for(EVENT_NAME, context, route=route)

    narration_paragraph, separator, rest = text.partition("\n\nStructured context:\n")
    assert separator, text
    assert narration_paragraph.endswith("…")
    # ``_bounded`` truncates the BODY to MAX_NARRATION_BYTES, then appends the
    # (3-byte UTF-8) marker on top -- the existing S5 contract, unchanged here.
    marker_bytes = len("…".encode("utf-8"))
    assert len(narration_paragraph.encode("utf-8")) <= MAX_NARRATION_BYTES + marker_bytes
    body, _, marker = narration_paragraph.rpartition("…")
    assert marker == ""
    assert len(body.encode("utf-8")) <= MAX_NARRATION_BYTES
    # The truncation marker replaces bytes ONLY in the rendered paragraph --
    # the structured object still travels, whole, on the second paragraph.
    assert rest == _canonical(context)


__all__: list[str] = []
