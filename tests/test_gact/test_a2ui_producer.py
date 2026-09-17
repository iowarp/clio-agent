"""A2UI producer tools (S4, docs/design/a2ui-compat-campaign-2026-09.md).

Round-trips the four producer tools (create/update-components/update-data-
model/delete) through the transcript-owned surface store, and proves every
producer mistake comes back as a typed refusal dict, never an exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_delete_a2ui_surface_tool,
    build_update_a2ui_components_tool,
    build_update_a2ui_data_model_tool,
)
from clio_agent.gact.app import build_app

WORKSPACE_ID = workspace_catalog_id()
BASIC_ID = basic_catalog_id()


def _session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="A2UI producer")
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session.id)
    return app, session.id


def _advertise_workspace_catalog(app: Any, session_id: str) -> None:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}})
    remember_client_capabilities(app, session_id, caps)


def _advertise_basic_catalog_only(app: Any, session_id: str) -> None:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [BASIC_ID]}})
    remember_client_capabilities(app, session_id, caps)


# ---- round trip: create -> update components -> update data model -> delete ------


def test_producer_round_trip_through_the_store(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    create = build_create_a2ui_surface_tool()
    update_components = build_update_a2ui_components_tool()
    update_data_model = build_update_a2ui_data_model_tool()
    delete = build_delete_a2ui_surface_tool()

    created = create(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "First"}],
    )
    assert created.get("ok") is not False
    assert created["rendered"] is True
    assert created["created"] is True
    assert created["catalog_id"] == WORKSPACE_ID

    updated = update_components(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "Second"}],
    )
    assert updated["rendered"] is True
    assert updated["created"] is False
    assert updated["revision"] > created["revision"]

    data_modeled = update_data_model(surface_id="round-trip", path="/x", value=42)
    assert data_modeled["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "round-trip")
    assert surface is not None
    assert surface.messages[-1]["updateDataModel"] == {
        "surfaceId": "round-trip",
        "path": "/x",
        "value": 42,
    }

    deleted = delete(surface_id="round-trip")
    assert deleted["rendered"] is True
    assert deleted["deleted"] is True
    assert deleted["state"] == "deleted"

    # The tombstone is immediately visible to this session's own store (a
    # revision cannot resurrect a deleted surface without a new create).
    tombstoned = app.state.a2ui_store.get(sid, "round-trip")
    assert tombstoned is not None
    assert tombstoned.state == "deleted"
    refused = update_components(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "Resurrected?"}],
    )
    assert refused["ok"] is False
    assert refused["reason"] == "a2ui_surface_not_found"


def test_update_data_model_delete_true_omits_the_value_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The protocol's own delete form: an omitted ``value`` key removes the path."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="deletable-key",
        components=[{"id": "root", "component": "Text", "text": "First"}],
        data_model={"ack": True},
    )

    result = build_update_a2ui_data_model_tool()(
        surface_id="deletable-key", path="/ack", delete=True
    )

    assert result["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "deletable-key")
    assert surface is not None
    last = surface.messages[-1]["updateDataModel"]
    assert last == {"surfaceId": "deletable-key", "path": "/ack"}
    assert "value" not in last


# ---- typed refusals, never exceptions ---------------------------------------------


def test_create_with_empty_catalog_id_and_no_advertisement_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)

    result = build_create_a2ui_surface_tool()(
        surface_id="unselectable",
        components=[{"id": "root", "component": "Text", "text": "First"}],
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_client_capabilities_unknown"
    assert result["hint"] == ""
    assert app.state.a2ui_store.get(sid, "unselectable") is None


def test_create_explicit_catalog_id_not_advertised_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """S4 adversarial-review fix: an explicit catalog_id for a NEW surface is
    a PREFERENCE, not a bypass -- it still must cross select_catalog's
    client-preference gate exactly like the empty-catalog_id default."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_basic_catalog_only(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="not-advertised",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result == {
        "ok": False,
        "reason": "a2ui_preferred_catalog_not_selectable",
        "detail": f"catalog_id {WORKSPACE_ID!r} is not selectable for this session",
        "hint": "",
    }
    assert app.state.a2ui_store.get(sid, "not-advertised") is None


def test_create_explicit_catalog_id_advertised_is_honoured(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The positive twin: the same explicit catalog_id succeeds once it is
    both client-advertised and producible."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="advertised",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result.get("ok") is not False
    assert result["rendered"] is True
    assert result["catalog_id"] == WORKSPACE_ID


def test_component_validation_failure_names_the_load_skill_hint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="bad-button",
        components=[{"id": "root", "component": "Button"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert result["hint"] == (
        'load_skill("a2ui-catalog-clio-workspace", file="catalog.json#/components/Button")'
    )
    assert app.state.a2ui_store.get(sid, "bad-button") is None


def test_update_components_on_an_unknown_surface_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _session(tmp_path, monkeypatch)

    result = build_update_a2ui_components_tool()(
        surface_id="never-created",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result == {
        "ok": False,
        "reason": "a2ui_surface_not_found",
        "detail": "A2UI surface not found: never-created",
        "hint": "",
    }


def test_update_data_model_on_a_deleted_surface_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="retired",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )
    build_delete_a2ui_surface_tool()(surface_id="retired")

    result = build_update_a2ui_data_model_tool()(surface_id="retired", path="/x", value=1)

    assert result["ok"] is False
    assert result["reason"] == "a2ui_surface_not_found"


def test_delete_an_unknown_surface_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _session(tmp_path, monkeypatch)

    result = build_delete_a2ui_surface_tool()(surface_id="ghost")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_surface_not_found"


def test_create_session_unavailable_is_a_typed_refusal(monkeypatch: Any) -> None:
    monkeypatch.setattr(gact_context, "active_app", lambda: None)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: "")

    result = build_create_a2ui_surface_tool()(
        surface_id="no-session", components=[{"id": "root", "component": "Text", "text": "x"}]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_session_unavailable"


def test_create_missing_root_component_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="no-root",
        components=[{"id": "not-root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert 'exactly one id="root"' in result["detail"]


# ---- docstrings carry no prop lore (S4 item 3) -------------------------------------


def test_producer_tool_docstrings_are_at_most_ten_lines_with_no_prop_lore() -> None:
    tools = [
        build_create_a2ui_surface_tool(),
        build_update_a2ui_components_tool(),
        build_update_a2ui_data_model_tool(),
        build_delete_a2ui_surface_tool(),
    ]
    # Deleted-docstring-wall content (a2ui_tools.py's 78 lines) named specific
    # component/property strings; none of that prose survives in the new tools.
    banned_substrings = (
        "clio.status.v1",
        "clio.map.v1",
        "clio.data-table.v1",
        "Tabs never use",
        "requires ``name``",
        "never accepts",
        "accessibility",
    )
    for tool in tools:
        doc = tool.func.__doc__ or ""
        lines = doc.strip().splitlines()
        assert len(lines) <= 10, f"{tool.name} docstring has {len(lines)} lines: {doc!r}"
        for banned in banned_substrings:
            assert banned not in doc, f"{tool.name} docstring still names {banned!r}"
