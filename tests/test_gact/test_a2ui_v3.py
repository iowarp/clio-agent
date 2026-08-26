"""A2UI 0.9.1 persistence, action, and security tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch, raises

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui import (
    MAX_A2UI_MESSAGES,
    A2UIValidationError,
    apply_batch,
    trusted_component_names,
    validate_server_message,
)
from clio_agent.gact.a2ui_tools import build_create_a2ui_surface_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.constants import A2UI_V091
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import Message

HEADERS = {
    "X-GACT-Version": "0.3",
    "X-A2UI-Version": "0.9.1",
}


def _session_client(tmp_path: Path) -> tuple[TestClient, str, Path]:
    sessions_path = tmp_path / "sessions.json"
    app = build_app(sessions_path=sessions_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="A2UI")
    return TestClient(app), session.id, sessions_path


def _create_message(surface_id: str = "surface_1") -> dict[str, object]:
    return {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": CLIO_A2UI_CATALOG_ID},
    }


def test_surface_lifecycle_persists_and_reconciles(tmp_path: Path) -> None:
    client, sid, sessions_path = _session_client(tmp_path)
    update = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {"id": "title", "component": "Text", "text": "Campaign status", "variant": "h3"},
                {
                    "id": "state",
                    "component": "clio.status.v1",
                    "label": "SPOTTER campaign",
                    "state": "running",
                    "detail": "4 runs checked",
                },
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), update], "correlation": {"run_id": "run_1"}},
    )

    assert response.status_code == 200
    surface = response.json()["surfaces"][-1]
    assert surface["state"] == "ready"
    assert surface["revision"] == 2
    assert surface["run_id"] == "run_1"

    rebuilt = build_app(sessions_path=sessions_path)
    persisted = rebuilt.state.a2ui_store.get(sid, "surface_1")
    assert persisted is not None
    assert persisted.revision == 2
    assert persisted.messages == [_create_message(), update]


def test_legacy_a2ui_ledger_is_retained_and_reported_as_superseded(tmp_path: Path) -> None:
    path = tmp_path / "a2ui-surfaces.json"
    original = '{"surfaces": [broken]}'
    path.write_text(original, encoding="utf-8")

    app = build_app(sessions_path=tmp_path / "sessions.json")
    store = app.state.a2ui_store

    assert store.list_wire("sess_any") == []
    assert path.read_text(encoding="utf-8") == original
    assert store.load_degradation == {
        "reason": "a2ui_ledger_superseded",
        "source_path": str(path),
        "replacement": "session_message_log",
    }

    session = app.state.sessions.create(workspace_id="ws_default", title="A2UI")
    response = TestClient(app).get(
        f"/v1/sessions/{session.id}/a2ui/surfaces",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["degradations"] == [store.load_degradation]


def test_message_batch_rejects_delete_followed_by_update_atomically(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    deleted_then_updated = [
        _create_message(),
        {"version": "v0.9.1", "deleteSurface": {"surfaceId": "surface_1"}},
        {
            "version": "v0.9.1",
            "updateDataModel": {
                "surfaceId": "surface_1",
                "path": "/late",
                "value": True,
            },
        },
    ]

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": deleted_then_updated},
    )

    assert response.status_code == 422
    assert "terminal within a message batch" in response.json()["error"]["message"]
    assert client.app.state.a2ui_store.get(sid, "surface_1") is None


def test_a2ui_message_eviction_preserves_create_and_reports_typed_count() -> None:
    messages = [_create_message()]
    messages.extend(
        {
            "version": "v0.9.1",
            "updateDataModel": {
                "surfaceId": "surface_1",
                "path": "/counter",
                "value": index,
            },
        }
        for index in range(MAX_A2UI_MESSAGES + 3)
    )

    surfaces, _ = apply_batch({}, "sess_1", messages)

    surface = surfaces[("sess_1", "surface_1")]
    assert len(surface.messages) == MAX_A2UI_MESSAGES
    assert "createSurface" in surface.messages[0]
    assert surface.eviction_reason == "a2ui_message_limit"
    assert surface.evicted_messages == 4


def test_complete_component_update_compacts_the_superseded_snapshot(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    first = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"id": "root", "component": "Text", "text": "First"}],
        },
    }
    corrected = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"id": "root", "component": "Text", "text": "Corrected"}],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), first, corrected]},
    )

    assert response.status_code == 200
    surface = response.json()["surfaces"][-1]
    assert surface["revision"] == 3
    assert surface["messages"] == [_create_message(), corrected]


def test_surface_ids_are_scoped_to_each_session(tmp_path: Path) -> None:
    client, first_sid, _ = _session_client(tmp_path)
    second = client.app.state.sessions.create(workspace_id="ws_default", title="Second A2UI")

    first = client.post(
        f"/v1/sessions/{first_sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("shared-surface-id")]},
    )
    second_response = client.post(
        f"/v1/sessions/{second.id}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("shared-surface-id")]},
    )

    assert first.status_code == 200
    assert second_response.status_code == 200
    assert client.app.state.a2ui_store.get(first_sid, "shared-surface-id") is not None
    assert client.app.state.a2ui_store.get(second.id, "shared-surface-id") is not None
    assert client.app.state.a2ui_store.list_wire(first_sid)[0]["session_id"] == first_sid
    assert client.app.state.a2ui_store.list_wire(second.id)[0]["session_id"] == second.id


def test_unknown_component_and_executable_url_are_rejected(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )
    unknown = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"id": "x", "component": "RawHtml", "html": "<script />"}],
        },
    }
    executable = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"id": "image", "component": "Image", "url": "javascript:alert(1)"}],
        },
    }

    unknown_response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [unknown]}
    )
    executable_response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [executable]},
    )

    assert unknown_response.status_code == 422
    assert unknown_response.json()["error"]["error"] == "a2ui_validation_failed"
    assert executable_response.status_code == 422


def test_renderer_required_component_properties_are_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    client, sid, _ = _session_client(tmp_path)
    invalid_artifact = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "root",
                    "component": "clio.artifact.v1",
                    "name": "station-plot.png",
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), invalid_artifact]},
    )

    assert response.status_code == 422
    assert "component shape is invalid" in response.json()["error"]["message"]
    assert client.app.state.a2ui_store.get(sid, "surface_1") is None


def test_catalog_models_and_generated_tool_guidance_share_one_allowlist() -> None:
    tool = build_create_a2ui_surface_tool()

    generated_allowlist = ", ".join(trusted_component_names())
    assert f"Trusted component names: {generated_allowlist}." in tool.desc
    assert "Checkbox" not in generated_allowlist
    assert "CheckBox" in generated_allowlist


def test_diff_paths_are_content_while_binding_paths_remain_json_pointers() -> None:
    for path in ("src/analysis.py", r"D:\\science\\analysis.py"):
        validate_server_message(
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "diff-surface",
                    "components": [
                        {
                            "id": "diff",
                            "component": "clio.diff.v1",
                            "path": path,
                            "diff": "@@ -1 +1 @@",
                        }
                    ],
                },
            }
        )

    with raises(A2UIValidationError, match="data bindings must use JSON Pointer paths"):
        validate_server_message(
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "binding-surface",
                    "components": [
                        {
                            "id": "field",
                            "component": "TextField",
                            "label": "Station",
                            "value": {"path": "station/name"},
                        }
                    ],
                },
            }
        )


def test_unknown_persisted_a2ui_version_is_quarantined_with_typed_reason() -> None:
    message = Message(
        id="msg_unknown_a2ui",
        session_id="sess_versioned",
        role="assistant",
        created_at="2026-08-26T12:00:00Z",
        updated_at="2026-08-26T12:00:00Z",
        parts=[
            Part(
                id="part_unknown_a2ui",
                type="a2ui",
                surface_id="surface_unknown",
                a2ui_protocol_version="9.9",
                a2ui_messages=[_create_message("surface_unknown")],
            )
        ],
    )

    projection = transcript_entities([message], "sess_versioned")

    assert projection["surfaces"] == []
    assert projection["a2ui_degradations"] == [
        {
            "code": "a2ui_persisted_version_unsupported",
            "reason": "A2UI part part_unknown_a2ui uses 9.9.",
            "part_id": "part_unknown_a2ui",
            "protocol_version": "9.9",
        }
    ]


def test_persisted_a2ui_part_carries_protocol_and_ordered_payload(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    messages = [
        _create_message("persisted-surface"),
        {
            "version": "v0.9.1",
            "updateComponents": {
                "surfaceId": "persisted-surface",
                "components": [{"id": "root", "component": "Text", "text": "Ready"}],
            },
        },
    ]

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": messages},
    )

    assert response.status_code == 200
    persisted = client.app.state.messages[sid][-1].parts[0]
    assert persisted.a2ui_protocol_version == A2UI_V091
    assert persisted.a2ui_messages == messages
    assert not (tmp_path / "a2ui-surfaces.json").exists()


def test_mermaid_component_is_trusted_but_executable_directives_are_rejected(
    tmp_path: Path,
) -> None:
    client, sid, _ = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )
    diagram = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "diagram",
                    "component": "clio.mermaid.v1",
                    "title": "Analysis flow",
                    "source": "flowchart LR\n  data --> model --> result",
                }
            ],
        },
    }
    dangerous = {
        **diagram,
        "updateComponents": {
            **diagram["updateComponents"],
            "components": [
                {
                    "id": "diagram",
                    "component": "clio.mermaid.v1",
                    "source": "flowchart LR\n click data javascript:alert(1)",
                }
            ],
        },
    }

    accepted = client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [diagram]}
    )
    rejected = client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [dangerous]}
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 422
    assert "executable or HTML directive" in rejected.json()["error"]["message"]


def test_registered_form_action_gets_a_server_surface_update(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "form.submit",
            "surfaceId": "surface_1",
            "sourceComponentId": "submit",
            "timestamp": "2026-08-22T12:00:00Z",
            "context": {"selection": "quarantine"},
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=HEADERS,
        json={"message": action, "correlation": {"run_id": "run_1"}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["submitted"] == {"selection": "quarantine"}
    messages = response.json()["surface"]["messages"]
    assert messages[-1]["updateDataModel"]["path"] == "/lastAction"


def test_unregistered_action_is_rejected(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "shell.execute",
            "surfaceId": "surface_1",
            "sourceComponentId": "danger",
            "timestamp": "2026-08-22T12:00:00Z",
            "context": {},
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_validation_failed"


def test_root_agent_tool_produces_surface_and_transcript_reference(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    tool = build_create_a2ui_surface_tool()

    result = tool(
        surface_id="luna-status",
        components=[
            {
                "id": "root",
                "component": "clio.status.v1",
                "label": "Luna producer",
                "state": "running",
            }
        ],
        data_model={"phase": "streaming"},
    )

    assert result["rendered"] is True
    assert result["revision"] == 3
    surface = app.state.a2ui_store.get(sid, "luna-status")
    assert surface is not None and surface.part_id == result["part_id"]
    parts = app.state.live_assistant_parts[sid]
    assert any(part.type == "a2ui" and part.surface_id == "luna-status" for part in parts)


def test_root_agent_tool_updates_one_stable_transcript_reference(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    tool = build_create_a2ui_surface_tool()

    first = tool(
        surface_id="stable-view",
        components=[{"id": "root", "component": "Text", "text": "First"}],
    )
    updated = tool(
        surface_id="stable-view",
        components=[{"id": "root", "component": "Text", "text": "Updated"}],
    )

    references = [
        part
        for part in app.state.live_assistant_parts[sid]
        if part.type == "a2ui" and part.surface_id == "stable-view"
    ]
    assert len(references) == 2
    assert updated["part_id"] == first["part_id"] == references[0].id
    assert references[1].metadata["projection_only"] is True
    surface = app.state.a2ui_store.get(sid, "stable-view")
    assert surface is not None
    assert surface.messages[-1]["updateComponents"]["components"][0]["text"] == "Updated"


def test_root_agent_tool_documents_the_valid_button_action_envelope() -> None:
    tool = build_create_a2ui_surface_tool()
    compact_description = " ".join(tool.desc.split())

    assert '"component": "Button", "child": "label-id", "action": {"event"' in compact_description
    assert (
        '"component": "Tabs", "tabs": [{"title": "Plot", "child": "plot-view"}'
        in compact_description
    )
    assert 'literal ``"root"`` because the official renderer mounts that id' in tool.desc
    assert "Tabs never use a ``children`` property" in tool.desc
    assert "``clio.data-table.v1`` does not accept ``title``" in tool.desc
    assert "Do not nest a second ``action`` object" in tool.desc
    compact_description = " ".join(tool.desc.split())
    assert "``agent.submit`` needs ``text`` or ``prompt``" in compact_description
    assert 'a server action has the shape ``{"event"' not in tool.desc
    assert "Accessibility is always an object, never a string" in tool.desc


def test_server_and_tool_reject_string_accessibility_before_persisting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)

    with raises(A2UIValidationError, match="accessibility must be an object"):
        build_create_a2ui_surface_tool()(
            surface_id="invalid-accessibility",
            components=[
                {
                    "id": "root",
                    "component": "Column",
                    "children": ["diagram"],
                    "accessibility": "Scientific workflow",
                },
                {
                    "id": "diagram",
                    "component": "clio.mermaid.v1",
                    "source": "flowchart LR\nA --> B",
                },
            ],
        )

    assert app.state.a2ui_store.get(sid, "invalid-accessibility") is None


def test_server_accepts_accessibility_object_and_data_binding(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={
            "messages": [
                _create_message("accessible-surface"),
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "accessible-surface",
                        "components": [
                            {
                                "id": "root",
                                "component": "Column",
                                "children": ["label"],
                                "accessibility": {
                                    "label": "Scientific workflow",
                                    "description": {"path": "/description"},
                                },
                            },
                            {"id": "label", "component": "Text", "text": "Ready"},
                        ],
                    },
                },
            ]
        },
    )

    assert response.status_code == 200


def test_server_rejects_a_double_wrapped_component_action(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    invalid = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {"id": "label", "component": "Text", "text": "Submit"},
                {
                    "id": "submit",
                    "component": "Button",
                    "child": "label",
                    "action": {"action": {"event": {"name": "form.submit", "context": {}}}},
                },
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), invalid]},
    )

    assert response.status_code == 422
    assert "A2UI action must have the shape" in response.json()["error"]["message"]


def test_server_rejects_agent_submit_without_prompt_context(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    invalid = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {"id": "label", "component": "Text", "text": "Continue"},
                {
                    "id": "continue",
                    "component": "Button",
                    "child": "label",
                    "action": {
                        "event": {
                            "name": "agent.submit",
                            "context": {"scope": "bounded_follow_up"},
                        }
                    },
                },
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), invalid]},
    )

    assert response.status_code == 422
    assert "requires context.text or context.prompt" in response.json()["error"]["message"]


def test_server_accepts_a_bounded_scientific_map(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    map_component = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "map",
                    "component": "clio.map.v1",
                    "title": "EarthScope stations",
                    "points": [
                        {
                            "id": "station_1",
                            "label": "Station 1",
                            "latitude": 41.88,
                            "longitude": -87.63,
                            "category": "GNSS",
                            "detail": "Illustrative station",
                        }
                    ],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), map_component]},
    )

    assert response.status_code == 200
    assert response.json()["surfaces"][-1]["state"] == "ready"


def test_server_rejects_out_of_range_map_coordinates(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    invalid = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "map",
                    "component": "clio.map.v1",
                    "points": [
                        {
                            "id": "station_1",
                            "label": "Invalid station",
                            "latitude": 91,
                            "longitude": -87.63,
                        }
                    ],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), invalid]},
    )

    assert response.status_code == 422
    assert "latitude is outside its valid range" in response.json()["error"]["message"]
    assert client.app.state.a2ui_store.get(sid, "surface_1") is None


def test_server_accepts_registered_csv_time_series(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    chart = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "root",
                    "component": "clio.time-series.v1",
                    "dataUri": "artifact://artifact_series_1",
                    "xKey": "time",
                    "yKeys": ["east", "north", "up"],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), chart]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["surfaces"][-1]["state"] == "ready"


def test_server_rejects_ambiguous_time_series_sources(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    invalid = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "root",
                    "component": "clio.time-series.v1",
                    "series": [{"time": 1, "east": 2}],
                    "dataUri": "artifact://artifact_series_1",
                    "xKey": "time",
                    "yKeys": ["east"],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(), invalid]},
    )

    assert response.status_code == 422
    assert "exactly one of series or dataUri" in response.json()["error"]["message"]
    assert client.app.state.a2ui_store.get(sid, "surface_1") is None


def test_root_agent_tool_rejects_batch_atomically(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    tool = build_create_a2ui_surface_tool()

    with raises(A2UIValidationError, match="component is not trusted"):
        tool(
            surface_id="invalid-surface",
            components=[{"id": "root", "component": {"type": "Column"}}],
        )

    assert app.state.a2ui_store.get(sid, "invalid-surface") is None


def test_root_agent_tool_requires_the_renderer_root_atomically(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    tool = build_create_a2ui_surface_tool()

    with raises(A2UIValidationError, match='exactly one id="root"'):
        tool(
            surface_id="missing-renderer-root",
            components=[
                {
                    "id": "root-tabs",
                    "component": "Tabs",
                    "tabs": [{"title": "Overview", "child": "overview"}],
                },
                {"id": "overview", "component": "Text", "text": "Overview"},
            ],
        )

    assert app.state.a2ui_store.get(sid, "missing-renderer-root") is None


def test_root_agent_tool_recreates_a_deleted_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    deleted = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={
            "messages": [
                _create_message("recreated-surface"),
                {
                    "version": "v0.9.1",
                    "deleteSurface": {"surfaceId": "recreated-surface"},
                },
            ]
        },
    )
    assert deleted.status_code == 200
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="recreated-surface",
        components=[
            {
                "id": "root",
                "component": "clio.status.v1",
                "label": "Recreated",
                "state": "completed",
            }
        ],
    )

    assert result["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "recreated-surface")
    assert surface is not None
    assert surface.state == "ready"
    assert [next(key for key in message if key != "version") for message in surface.messages] == [
        "createSurface",
        "updateComponents",
    ]
