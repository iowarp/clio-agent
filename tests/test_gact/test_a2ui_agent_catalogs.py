"""Per-agent A2UI catalog allowlists (v15 S8).

An agent's ``a2ui_catalogs`` is the COMPLETE, ordered allowlist of catalogs it
may produce against: nothing is implicit, the builtins included. Resolution is
an ordered union over declaration sources
(``a2ui_catalogs/declarations.py::resolve_agent_catalogs``) -- today one
source, the active blueprint; agent-plugins add more later. Every consumer
that decides producibility or disclosure derives from that one resolution.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient

from clio_agent.gact import agent_blueprint_requires
from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import (
    agent_capabilities,
    remember_client_capabilities,
    select_catalog,
)
from clio_agent.gact.a2ui_catalogs.activation import (
    resolve_session_catalogs,
    session_a2ui_producers_enabled,
    session_producible_catalog_ids,
)
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.declarations import (
    CatalogDeclarationSource,
    parse_catalog_declarations,
    resolve_agent_catalogs,
)
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.skill_runtime import skill_runtime_for_agent
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef

from .a2ui_catalog_binding import (
    BUILTINS_PACK,
    FIXTURE_PACKS,
    bind_builtin_catalogs,
    bind_session_blueprint,
)

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
WORKSPACE_ID = workspace_catalog_id()
BASIC_ID = basic_catalog_id()
MINIMAL_ID = "https://example.test/a2ui/catalogs/minimal"
MARKETPLACE = Path(__file__).resolve().parents[2] / "external" / "clio-agent-marketplace"
EARTHSCOPE_ID = "https://iowarp.ai/a2ui/catalogs/earthscope-stations/v1"
PRODUCER_TOOLS = {
    "create_a2ui_surface",
    "update_a2ui_components",
    "update_a2ui_data_model",
    "delete_a2ui_surface",
}


def _source(unit: str, raw: Any, root: Path) -> CatalogDeclarationSource:
    return parse_catalog_declarations(raw, unit_kind="blueprint", unit_id=unit, root=root)


def _app_session(tmp_path: Path) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="per-agent catalogs")
    return app, session.id


def _advertise(app: Any, session_id: str, catalog_ids: list[str]) -> None:
    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": catalog_ids}})
    remember_client_capabilities(app, session_id, caps)


def _root_agent() -> AgentDef:
    return AgentDef(id="root", title="Root", tier=1, module={"kind": "react"})


def _at_pack_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run at the marketplace packs' declared floor (a pre-release checkout is below it)."""

    monkeypatch.setattr(agent_blueprint_requires, "_running_clio_agent_version", lambda: "0.9.4.17")


def _reasons(app: Any, session_id: str) -> list[str]:
    return [row["reason"] for row in app.state.a2ui_catalogs.session_reasons(session_id)]


# --------------------------------------------------------------------------- #
# resolve_agent_catalogs: ordered union, dedupe, conflict                     #
# --------------------------------------------------------------------------- #


def test_resolution_is_an_ordered_union_over_sources_with_dedupe() -> None:
    first = _source(
        "blueprint:a",
        ["clio-workspace", {"minimal": "catalogs/minimal"}],
        FIXTURE_PACKS / "minimal",
    )
    second = _source(
        "plugin:b",
        ["basic", "clio-workspace", {"minimal": "catalogs/minimal"}],
        FIXTURE_PACKS / "minimal",
    )

    resolved = resolve_agent_catalogs([first, second], record=False)

    assert resolved.catalog_ids == (WORKSPACE_ID, MINIMAL_ID, BASIC_ID)
    assert resolved.issues == ()
    assert resolved.declared is True


def test_resolution_preserves_written_order_not_alphabetical() -> None:
    resolved = resolve_agent_catalogs(
        [_source("blueprint:a", ["clio-workspace", "basic"], FIXTURE_PACKS)], record=False
    )
    assert resolved.catalog_ids == (WORKSPACE_ID, BASIC_ID)
    reverse = resolve_agent_catalogs(
        [_source("blueprint:a", ["basic", "clio-workspace"], FIXTURE_PACKS)], record=False
    )
    assert reverse.catalog_ids == (BASIC_ID, WORKSPACE_ID)


def test_same_name_different_origin_is_a_typed_conflict_never_an_override() -> None:
    first = _source("blueprint:a", [{"minimal": "catalogs/minimal"}], FIXTURE_PACKS / "minimal")
    # Same name, a different directory (a different catalog entirely).
    second = _source("plugin:b", [{"minimal": "catalogs/second"}], FIXTURE_PACKS / "second")

    resolved = resolve_agent_catalogs([first, second], record=False)

    assert resolved.catalog_ids == (MINIMAL_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]
    assert resolved.issues[0].unit_id == "plugin:b"


def test_builtin_name_redeclared_as_a_directory_is_a_conflict() -> None:
    first = _source("blueprint:a", ["clio-workspace"], FIXTURE_PACKS)
    second = _source(
        "plugin:b", [{"clio-workspace": "catalogs/minimal"}], FIXTURE_PACKS / "minimal"
    )

    resolved = resolve_agent_catalogs([first, second], record=False)

    assert resolved.catalog_ids == (WORKSPACE_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]


def test_two_names_resolving_to_one_catalog_id_is_a_conflict() -> None:
    source = _source(
        "blueprint:a",
        [{"minimal": "catalogs/minimal"}, {"alias": "catalogs/minimal"}],
        FIXTURE_PACKS / "minimal",
    )

    resolved = resolve_agent_catalogs([source], record=False)

    assert resolved.catalog_ids == (MINIMAL_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]


def test_unknown_builtin_and_malformed_entries_are_typed_issues() -> None:
    source = _source("blueprint:a", ["clio-workspce", 42, {"a": "x", "b": "y"}], FIXTURE_PACKS)

    resolved = resolve_agent_catalogs([source], record=False)

    assert resolved.catalog_ids == ()
    assert [issue.reason for issue in resolved.issues] == [
        "a2ui_catalog_builtin_unknown",
        "a2ui_catalog_declaration_invalid",
        "a2ui_catalog_declaration_invalid",
    ]
    assert resolved.empty_reason() == "a2ui_no_catalogs_resolved"


def test_no_declaration_resolves_to_nothing_with_its_own_reason() -> None:
    assert resolve_agent_catalogs([]).empty_reason() == "a2ui_no_catalogs_declared"
    undeclared = _source("blueprint:a", None, FIXTURE_PACKS)
    assert undeclared.declared is False
    assert resolve_agent_catalogs([undeclared]).empty_reason() == "a2ui_no_catalogs_declared"


def test_legacy_mapping_form_is_pack_local_only_in_written_order() -> None:
    source = _source("blueprint:a", {"minimal": "catalogs/minimal"}, FIXTURE_PACKS / "minimal")
    assert resolve_agent_catalogs([source], record=False).catalog_ids == (MINIMAL_ID,)


# --------------------------------------------------------------------------- #
# Blueprint validation refuses bad declarations                                #
# --------------------------------------------------------------------------- #


def _pack_with(tmp_path: Path, catalogs_yaml: str) -> Path:
    root = tmp_path / "pack"
    shutil.copytree(FIXTURE_PACKS / "minimal", root)
    agent_md = root / "AGENT.md"
    text = agent_md.read_text(encoding="utf-8")
    agent_md.write_text(
        text.replace("a2ui_catalogs:\n  minimal: catalogs/minimal\n", catalogs_yaml),
        encoding="utf-8",
    )
    return root


def test_validation_refuses_an_unknown_builtin_name(tmp_path: Path) -> None:
    root = _pack_with(tmp_path, "a2ui_catalogs:\n  - clio-wrkspace\n")
    result = validate_agent_blueprint_path(root, scope="session")
    assert result["enabled"] is False
    assert any("clio-wrkspace" in error for error in result["validation_errors"])


def test_validation_refuses_a_missing_pack_directory(tmp_path: Path) -> None:
    root = _pack_with(tmp_path, "a2ui_catalogs:\n  - absent: catalogs/absent\n")
    result = validate_agent_blueprint_path(root, scope="session")
    assert result["enabled"] is False
    assert any("absent" in error and "missing" in error for error in result["validation_errors"])


def test_validation_accepts_builtins_and_pack_catalogs_together(tmp_path: Path) -> None:
    root = _pack_with(
        tmp_path, "a2ui_catalogs:\n  - minimal: catalogs/minimal\n  - clio-workspace\n"
    )
    result = validate_agent_blueprint_path(root, scope="session")
    assert result["validation_errors"] == []
    assert result["enabled"] is True


# --------------------------------------------------------------------------- #
# No declaration: no catalogs, no producer tools, typed reason                 #
# --------------------------------------------------------------------------- #


def test_undeclared_agent_has_no_catalogs_no_tools_and_a_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid = _app_session(tmp_path)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)

    assert session_producible_catalog_ids(app, sid) == []
    names = {getattr(tool, "name", "") for tool in build_auto_react_tools(_root_agent())}
    assert not names & PRODUCER_TOOLS
    assert "a2ui_no_catalogs_declared" in _reasons(app, sid)

    runtime = skill_runtime_for_agent(app, _root_agent(), session_id=sid)
    assert not [skill_id for skill_id in runtime.resolutions if skill_id.startswith("a2ui-")]
    assert "a2ui-catalog" not in runtime.prompt_block


def test_undeclared_agent_producer_refuses_with_the_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that declared the tool explicitly still has nothing to produce against."""

    app, sid = _app_session(tmp_path)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    _advertise(app, sid, [WORKSPACE_ID, BASIC_ID])

    result = build_create_a2ui_surface_tool()(
        surface_id="s", components=[{"id": "root", "component": "Text", "text": "x"}]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_no_catalogs_declared"
    assert result["hint"]


def test_declared_agent_gets_producer_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid = _app_session(tmp_path)
    bind_builtin_catalogs(app, sid)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)

    assert session_a2ui_producers_enabled(app, sid) is True
    names = {getattr(tool, "name", "") for tool in build_auto_react_tools(_root_agent())}
    assert PRODUCER_TOOLS <= names


# --------------------------------------------------------------------------- #
# Shipped agents: base-agent and EarthScope                                   #
# --------------------------------------------------------------------------- #


def test_base_agent_gets_exactly_clio_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at_pack_floor(monkeypatch)
    app, sid = _app_session(tmp_path)
    bind_session_blueprint(app, sid, MARKETPLACE / "base-agent", "base-agent")

    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID]
    runtime = skill_runtime_for_agent(app, _root_agent(), session_id=sid)
    catalog_skills = [skill_id for skill_id in runtime.resolutions if skill_id.startswith("a2ui-")]
    assert catalog_skills == ["a2ui-catalog-clio-workspace"]


def test_earthscope_gets_clio_workspace_then_its_own_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at_pack_floor(monkeypatch)
    app, sid = _app_session(tmp_path)
    bind_session_blueprint(
        app, sid, MARKETPLACE / "earthscope-single-agent", "earthscope-single-agent"
    )

    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID, EARTHSCOPE_ID]
    assert agent_capabilities(app, sid)["v0.9"]["supportedCatalogIds"] == [
        WORKSPACE_ID,
        EARTHSCOPE_ID,
    ]


def test_shipped_marketplace_packs_validate_with_their_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at_pack_floor(monkeypatch)
    for agent_md in sorted(MARKETPLACE.glob("*/AGENT.md")):
        pack = agent_md.parent.name
        result = validate_agent_blueprint_path(MARKETPLACE / pack, scope="session")
        # Runtime-tool references (view_image, ...) need a live app; this test is
        # about the catalog declarations, which must add no error of their own.
        assert [e for e in result["validation_errors"] if "a2ui" in e] == [], pack


# --------------------------------------------------------------------------- #
# Selection, producibility, and the routes follow the declared order          #
# --------------------------------------------------------------------------- #


def test_unnamed_surface_selects_the_first_declared_catalog_the_client_supports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client lists Basic first; the agent declared clio-workspace first."""

    app, sid = _app_session(tmp_path)
    bind_builtin_catalogs(app, sid)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    _advertise(app, sid, [BASIC_ID, WORKSPACE_ID])

    assert select_catalog(app, sid).catalog_id == WORKSPACE_ID
    created = build_create_a2ui_surface_tool()(
        surface_id="unnamed", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    assert created["catalog_id"] == WORKSPACE_ID


def test_basic_is_not_producible_unless_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at_pack_floor(monkeypatch)
    app, sid = _app_session(tmp_path)
    bind_session_blueprint(app, sid, MARKETPLACE / "base-agent", "base-agent")
    _advertise(app, sid, [BASIC_ID])

    assert BASIC_ID not in session_producible_catalog_ids(app, sid)
    assert app.state.a2ui_catalogs.get(BASIC_ID) is not None  # still installed/renderable
    assert select_catalog(app, sid).reason == "a2ui_catalog_no_client_match"
    with TestClient(app) as client:
        response = client.post(
            f"/v1/sessions/{sid}/a2ui/messages",
            headers=HEADERS,
            json={
                "messages": [
                    {
                        "version": "v0.9.1",
                        "createSurface": {"surfaceId": "b", "catalogId": BASIC_ID},
                    }
                ]
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_catalog_not_producible"


def test_capabilities_and_catalogs_routes_follow_declared_order(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
    reversed_pack = tmp_path / "reversed"
    shutil.copytree(BUILTINS_PACK, reversed_pack)
    agent_md = reversed_pack / "AGENT.md"
    agent_md.write_text(
        agent_md.read_text(encoding="utf-8").replace(
            "  - clio-workspace\n  - basic\n", "  - basic\n  - clio-workspace\n"
        ),
        encoding="utf-8",
    )
    bind_session_blueprint(app, sid, reversed_pack, "a2ui-builtins-pack")

    with TestClient(app) as client:
        caps = client.get(f"/v1/sessions/{sid}/a2ui/capabilities", headers=HEADERS)
        catalogs = client.get(f"/v1/sessions/{sid}/a2ui/catalogs", headers=HEADERS)
    assert caps.status_code == 200, caps.text
    assert caps.json()["agent"]["v0.9"]["supportedCatalogIds"] == [BASIC_ID, WORKSPACE_ID]
    assert caps.json()["selection"]["producible_catalog_ids"] == [BASIC_ID, WORKSPACE_ID]
    rows = catalogs.json()["catalogs"]
    assert [row["catalogId"] for row in rows if row["producible"]] == [BASIC_ID, WORKSPACE_ID]


def test_undeclared_session_routes_report_no_producible_catalogs(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
    with TestClient(app) as client:
        caps = client.get(f"/v1/sessions/{sid}/a2ui/capabilities", headers=HEADERS)
        catalogs = client.get(f"/v1/sessions/{sid}/a2ui/catalogs", headers=HEADERS)
    assert caps.json()["agent"]["v0.9"]["supportedCatalogIds"] == []
    assert caps.json()["selection"]["reason"] == "a2ui_no_catalogs_declared"
    rows = catalogs.json()["catalogs"]
    assert rows and not any(row["producible"] for row in rows)  # installed, none producible


def test_one_resolution_backs_every_consumer(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
    bind_builtin_catalogs(app, sid)
    resolved = resolve_session_catalogs(app, sid)
    assert list(resolved.catalog_ids) == session_producible_catalog_ids(app, sid)
    assert agent_capabilities(app, sid)["v0.9"]["supportedCatalogIds"] == list(resolved.catalog_ids)
