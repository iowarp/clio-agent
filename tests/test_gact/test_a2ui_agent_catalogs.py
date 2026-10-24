"""Per-agent A2UI catalog allowlists, declared by the agent's resolved plugins.

An agent's catalogs are the COMPLETE, ordered allowlist it may produce against.
They are the ``catalogs`` rows of its pinned resolution -- ``clio-workspace``
from ``core``, then whatever its other plugins name or ship, in plugin order --
resolved as an ordered union over one declaration source per plugin
(``a2ui_catalogs/declarations.py``). Every consumer that decides producibility
or disclosure derives from that one resolution
(``activation.resolve_session_catalogs``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import (
    agent_capabilities,
    remember_client_capabilities,
    resolution_catalog_ids,
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
    plugin_catalog_sources,
    resolve_agent_catalogs,
)
from clio_agent.gact.a2ui_catalogs.plugin import load_catalog_directory
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.agents.resolution import (
    pin_session_agent,
    session_agent_def,
    session_resolution,
)
from clio_agent.gact.agents.skill_runtime import skill_runtime_for_agent
from clio_agent.gact.app import build_app
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.resolve import Delta
from tests._marketplace import MARKETPLACE_ROOT
from tests._plugin_fixtures import custom_marketplace

from .a2ui_catalog_binding import (
    MINIMAL_CATALOG_ID,
    OWN_CATALOG_ID,
    OWN_PLUGIN,
    bind_builtin_catalogs,
    bind_session_plugins,
    catalog_dir,
)

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
WORKSPACE_ID = workspace_catalog_id()
BASIC_ID = basic_catalog_id()
MINIMAL_DIR = catalog_dir("a2ui-minimal", "minimal")
SECOND_DIR = catalog_dir("a2ui-second", "second")
# Shipped plugins whose catalogs the shipped-marketplace test must see; guards
# against a vacuous pass.
SHIPPED_PLUGINS_WITH_CATALOGS = ("earthscope", "abaqus")
PRODUCER_TOOLS = {
    "create_a2ui_surface",
    "update_a2ui_components",
    "update_a2ui_data_model",
    "delete_a2ui_surface",
}


def _row(plugin: str, name: str, path: Path | None = None) -> dict[str, str]:
    """One resolution ``catalogs`` row: a builtin name, or a plugin's catalog folder."""

    return {"id": name, "plugin": plugin, "path": str(path) if path else ""}


def _resolve(*rows: dict[str, str]) -> Any:
    return resolve_agent_catalogs(plugin_catalog_sources(list(rows), []), record=False)


def _source(unit: str, raw: Any, root: Path) -> CatalogDeclarationSource:
    return parse_catalog_declarations(raw, unit_kind="plugin", unit_id=unit, root=root)


def _app_session(tmp_path: Path) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    # ws_default seeds root_path=os.getcwd() (workspaces.py _seed_default) --
    # rebind it to this test's own tmp_path so a producer-tool call in this
    # file (e.g. a minted surface-definition artifact, #1533 S4) never writes
    # through it into the real invocation cwd.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    session = app.state.sessions.create(workspace_id="ws_default", title="per-agent catalogs")
    return app, session.id


def _advertise(app: Any, session_id: str, catalog_ids: list[str]) -> None:
    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": catalog_ids}})
    remember_client_capabilities(app, session_id, caps)


def _reasons(app: Any, session_id: str) -> list[str]:
    return [row["reason"] for row in app.state.a2ui_catalogs.session_reasons(session_id)]


def _plugin_with(tmp_path: Path, plugin_id: str, ext: dict[str, Any]) -> Path:
    """A catalog-only native plugin folder under ``tmp_path/plugins``."""

    root = tmp_path / "plugins" / plugin_id
    root.mkdir(parents=True)
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": plugin_id,
        "version": "1.0.0",
        "extensions": {"ai.iowarp.clio": ext},
    }
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _catalog_skills(app: Any, session_id: str) -> list[str]:
    runtime = skill_runtime_for_agent(
        app, session_agent_def(app, session_id), session_id=session_id
    )
    return [skill_id for skill_id in runtime.resolutions if skill_id.startswith("a2ui-")]


# --------------------------------------------------------------------------- #
# resolve_agent_catalogs over plugin sources: ordered union, dedupe, conflict #
# --------------------------------------------------------------------------- #


def test_plugin_sources_are_one_per_plugin_in_resolution_order() -> None:
    sources = plugin_catalog_sources(
        [
            _row("core", "clio-workspace"),
            _row("visuals", "minimal", MINIMAL_DIR),
            _row("extra", "basic"),
        ],
        [{"id": "visuals", "executable_digest": "e1", "text_digest": "t1"}],
    )

    by_unit = {source.unit_id: source.install_checksum for source in sources}
    # A source carries its plugin's install checksum when the resolution lists it.
    assert by_unit["plugin:visuals"] and not by_unit["plugin:core"]
    assert [source.unit_id for source in sources] == [
        "plugin:core",
        "plugin:visuals",
        "plugin:extra",
    ]
    assert [d.origin for s in sources for d in s.declarations] == [
        "builtin",
        "directory",
        "builtin",
    ]
    assert all(source.declared and source.unit_kind == "plugin" for source in sources)


def test_resolution_is_an_ordered_union_over_plugins_with_dedupe() -> None:
    resolved = _resolve(
        _row("core", "clio-workspace"),
        _row("a", "minimal", MINIMAL_DIR),
        _row("b", "basic"),
        _row("b", "clio-workspace"),
        _row("b", "minimal", MINIMAL_DIR),
    )

    assert resolved.catalog_ids == (WORKSPACE_ID, MINIMAL_CATALOG_ID, BASIC_ID)
    assert resolved.issues == ()
    assert resolved.declared is True


def test_resolution_preserves_plugin_order_not_alphabetical() -> None:
    assert _resolve(_row("a", "clio-workspace"), _row("b", "basic")).catalog_ids == (
        WORKSPACE_ID,
        BASIC_ID,
    )
    assert _resolve(_row("a", "basic"), _row("b", "clio-workspace")).catalog_ids == (
        BASIC_ID,
        WORKSPACE_ID,
    )


def test_same_name_different_origin_is_a_typed_conflict_never_an_override() -> None:
    # Same name, a different folder (a different catalog entirely).
    resolved = _resolve(_row("a", "minimal", MINIMAL_DIR), _row("b", "minimal", SECOND_DIR))

    assert resolved.catalog_ids == (MINIMAL_CATALOG_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]
    assert resolved.issues[0].unit_id == "plugin:b"


def test_builtin_name_shipped_as_a_folder_is_a_conflict() -> None:
    resolved = _resolve(_row("core", "clio-workspace"), _row("b", "clio-workspace", MINIMAL_DIR))

    assert resolved.catalog_ids == (WORKSPACE_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]
    assert resolved.issues[0].unit_id == "plugin:b"


def test_two_names_resolving_to_one_catalog_id_is_a_conflict() -> None:
    resolved = _resolve(_row("a", "minimal", MINIMAL_DIR), _row("b", "alias", MINIMAL_DIR))

    assert resolved.catalog_ids == (MINIMAL_CATALOG_ID,)
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_declaration_conflict"]


def test_unknown_builtin_name_is_a_typed_issue() -> None:
    resolved = _resolve(_row("a", "clio-workspce"))

    assert resolved.catalog_ids == ()
    assert [issue.reason for issue in resolved.issues] == ["a2ui_catalog_builtin_unknown"]
    assert resolved.issues[0].unit_id == "plugin:a"
    assert resolved.empty_reason() == "a2ui_no_catalogs_resolved"


def test_a_shipped_catalog_folder_missing_files_is_refused_with_a_typed_issue(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "absent"
    broken.mkdir()
    shutil.copy(MINIMAL_DIR / "catalog.json", broken / "catalog.json")

    resolved = _resolve(_row("core", "clio-workspace"), _row("a", "absent", broken))

    assert resolved.catalog_ids == (WORKSPACE_ID,)
    [issue] = resolved.issues
    assert issue.reason == "a2ui_catalog_declaration_invalid"
    assert "absent" in issue.detail and "missing" in issue.detail
    entry, errors = load_catalog_directory(broken, "absent")
    assert entry is None
    assert any("catalog.clio.json" in error for error in errors)


def test_malformed_raw_declarations_are_typed_issues() -> None:
    source = _source("plugin:a", ["clio-workspce", 42, {"a": "x", "b": "y"}], MINIMAL_DIR)

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
    undeclared = _source("plugin:a", None, MINIMAL_DIR)
    assert undeclared.declared is False
    assert resolve_agent_catalogs([undeclared]).empty_reason() == "a2ui_no_catalogs_declared"


def test_explicit_empty_list_is_no_catalogs_declared() -> None:
    empty = _source("plugin:a", [], MINIMAL_DIR)
    assert empty.declared is True
    assert resolve_agent_catalogs([empty]).empty_reason() == "a2ui_no_catalogs_declared"


def test_mapping_form_declares_folders_in_written_order() -> None:
    source = _source("plugin:a", {"minimal": "."}, MINIMAL_DIR)
    assert resolve_agent_catalogs([source], record=False).catalog_ids == (MINIMAL_CATALOG_ID,)


# --------------------------------------------------------------------------- #
# The plugin loader turns a plugin's declarations into resolution rows        #
# --------------------------------------------------------------------------- #


def test_loader_reads_named_builtins_then_shipped_catalog_folders(tmp_path: Path) -> None:
    root = _plugin_with(tmp_path, "both", {"catalogs": ["basic"]})
    shutil.copytree(MINIMAL_DIR, root / "ai.iowarp.clio" / "a2ui" / "minimal")
    # A folder without catalog.json is not a catalog.
    (root / "ai.iowarp.clio" / "a2ui" / "notes").mkdir()

    record = load_plugin(root, marketplace="test", trusted=False)

    assert [(row.id, bool(row.path)) for row in record.catalogs] == [
        ("basic", False),
        ("minimal", True),
    ]
    resolved = _resolve(
        *(_row(record.id, row.id, Path(row.path) if row.path else None) for row in record.catalogs)
    )
    assert resolved.catalog_ids == (BASIC_ID, MINIMAL_CATALOG_ID)


# --------------------------------------------------------------------------- #
# Producer tools follow the plugins that claim them and a resolved catalog    #
# --------------------------------------------------------------------------- #


def test_standard_agent_gets_producer_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid = _app_session(tmp_path)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    resolution = session_resolution(app, sid)
    agent = session_agent_def(app, sid)

    assert session_a2ui_producers_enabled(app, sid) is True
    assert PRODUCER_TOOLS <= set(agent.tools)
    available, _gateway, withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True, a2ui_catalogs=bool(resolution.catalogs)
    )
    assert PRODUCER_TOOLS <= set(available)
    assert not PRODUCER_TOOLS & set(withheld)


def test_producer_tools_are_withheld_with_a_reason_when_no_catalog_resolves() -> None:
    from clio_agent.gact.types import AgentDef

    agent = AgentDef(id="producer", title="Producer", tools=sorted(PRODUCER_TOOLS))

    available, _gateway, withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True, a2ui_catalogs=False
    )

    assert not PRODUCER_TOOLS & set(available)
    assert withheld == dict.fromkeys(sorted(PRODUCER_TOOLS), "no_a2ui_catalog_resolved")


def test_agent_without_the_visuals_plugin_has_no_producer_tools_or_catalog_skills(
    tmp_path: Path,
) -> None:
    """The producer tools come only from a plugin that claims them (``visuals``)."""

    app, sid = _app_session(tmp_path)
    resolution = pin_session_agent(app, sid, delta=Delta(remove=("visuals",)))

    assert "visuals" not in resolution.plugin_ids
    # The catalog itself still resolves (core declares it) ...
    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID]
    # ... but nothing in this agent can produce against it, so nothing discloses it.
    assert not PRODUCER_TOOLS & set(session_agent_def(app, sid).tools)
    assert _catalog_skills(app, sid) == []


# --------------------------------------------------------------------------- #
# The shipped declaration shapes: core only, and core then a plugin's own     #
# --------------------------------------------------------------------------- #


def test_standard_agent_gets_exactly_clio_workspace(tmp_path: Path) -> None:
    """``clio/standard``: only ``core`` declares a catalog, so ``[clio-workspace]``."""

    app, sid = _app_session(tmp_path)

    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID]
    assert _catalog_skills(app, sid) == ["a2ui-catalog-clio-workspace"]


def test_agent_gets_clio_workspace_then_its_plugins_own_catalog(tmp_path: Path) -> None:
    """The earthscope shape: core's builtin first, then the catalog a plugin ships."""

    app, sid = _app_session(tmp_path)
    bind_session_plugins(app, sid, OWN_PLUGIN)

    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID, OWN_CATALOG_ID]
    assert agent_capabilities(app, sid)["v0.9"]["supportedCatalogIds"] == [
        WORKSPACE_ID,
        OWN_CATALOG_ID,
    ]
    assert _catalog_skills(app, sid) == ["a2ui-catalog-clio-workspace", "a2ui-catalog-own"]


@pytest.mark.marketplace
def test_shipped_marketplace_plugin_catalogs_load() -> None:
    """The one test here about SHIPPED content: the pinned marketplace submodule.

    It cannot be self-contained (its subject is the submodule), so its requirement is
    explicit: CI checks out submodules, and a local checkout without it fails here with
    the command that fixes it, instead of globbing an empty directory and passing.
    """

    plugins = MARKETPLACE_ROOT / "plugins"
    missing = [p for p in SHIPPED_PLUGINS_WITH_CATALOGS if not (plugins / p).is_dir()]
    assert not missing, f"the pinned marketplace no longer ships {missing}"
    with_catalogs: list[str] = []
    for manifest in sorted(plugins.glob("*/plugin.json")):
        record = load_plugin(manifest.parent, marketplace="clio", trusted=False)
        rows = [
            _row(record.id, row.id, Path(row.path) if row.path else None) for row in record.catalogs
        ]
        if not rows:
            continue
        with_catalogs.append(record.id)
        resolved = _resolve(*rows)
        assert resolved.issues == (), (record.id, resolved.issues)
        assert len(resolved.catalog_ids) == len(rows), record.id
    assert set(SHIPPED_PLUGINS_WITH_CATALOGS) <= set(with_catalogs)


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


def test_basic_is_not_producible_unless_declared(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
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


_BASIC_FIRST_AGENT = """
from clio_agent.sdk import Agent


class BasicFirst(Agent):
    id = "basic-first"
    version = "1.0.0"
    name = "Basic first"
    description = "Lists a plugin naming Basic before core."
    plugins = ["basic-first-catalog", "core", "visuals"]
"""


def test_capabilities_and_catalogs_routes_follow_plugin_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"basic-first-catalog": {"ext": {"catalogs": ["basic"]}}},
        agents={"basic_first.py": _BASIC_FIRST_AGENT},
    )
    app = build_app(sessions_path=tmp_path / "sessions.json")

    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            json={"title": "basic first", "metadata": {"agent_ref": "clio/basic-first"}},
        )
        assert created.status_code in (200, 201), created.text
        sid = created.json()["id"]
        caps = client.get(f"/v1/sessions/{sid}/a2ui/capabilities", headers=HEADERS)
        catalogs = client.get(f"/v1/sessions/{sid}/a2ui/catalogs", headers=HEADERS)
    assert caps.status_code == 200, caps.text
    assert caps.json()["agent"]["v0.9"]["supportedCatalogIds"] == [BASIC_ID, WORKSPACE_ID]
    assert caps.json()["selection"]["producible_catalog_ids"] == [BASIC_ID, WORKSPACE_ID]
    rows = catalogs.json()["catalogs"]
    assert [row["catalogId"] for row in rows if row["producible"]] == [BASIC_ID, WORKSPACE_ID]


def test_one_resolution_backs_every_consumer(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
    pinned = bind_builtin_catalogs(app, sid)
    resolved = resolve_session_catalogs(app, sid)
    assert resolved.catalog_ids == (WORKSPACE_ID, BASIC_ID)
    assert list(resolved.catalog_ids) == session_producible_catalog_ids(app, sid)
    assert agent_capabilities(app, sid)["v0.9"]["supportedCatalogIds"] == list(resolved.catalog_ids)
    assert resolution_catalog_ids(pinned) == list(resolved.catalog_ids)


# --------------------------------------------------------------------------- #
# A default session (clio/standard) declares clio-workspace                   #
# --------------------------------------------------------------------------- #


def test_default_session_produces_clio_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL default path: a session created over HTTP with no agent runs
    ``clio/standard``, whose only catalog is core's ``clio-workspace``."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post("/v1/sessions", json={"title": "default"})
        assert created.status_code in (200, 201), created.text
        sid = created.json()["id"]
        caps = client.get(f"/v1/sessions/{sid}/a2ui/capabilities", headers=HEADERS)
    assert caps.json()["agent"]["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID]
    assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID]
    assert session_a2ui_producers_enabled(app, sid) is True
    resolution = session_resolution(app, sid)
    assert resolution.agent["ref"] == "clio/standard"
    assert resolution.catalogs == [{"id": "clio-workspace", "plugin": "core", "path": ""}]
    assert PRODUCER_TOOLS <= set(session_agent_def(app, sid).tools)


# --------------------------------------------------------------------------- #
# Resolution issues are recorded once, not once per call                      #
# --------------------------------------------------------------------------- #


def test_resolution_issue_is_recorded_once_per_session(tmp_path: Path) -> None:
    app, sid = _app_session(tmp_path)
    _plugin_with(tmp_path, "misspelled", {"catalogs": ["bassic"]})
    bind_session_plugins(app, sid, "misspelled", root=tmp_path / "plugins")

    for _ in range(5):
        assert session_producible_catalog_ids(app, sid) == [WORKSPACE_ID]

    assert _reasons(app, sid).count("a2ui_catalog_builtin_unknown") == 1


# --------------------------------------------------------------------------- #
# The generated catalog skill states each catalog's id and default role       #
# --------------------------------------------------------------------------- #


def test_generated_catalog_skills_state_catalog_id_and_default_role(tmp_path: Path) -> None:
    from clio_agent.gact.skills import SkillCatalog

    app, sid = _app_session(tmp_path)
    bind_builtin_catalogs(app, sid)  # declares [clio-workspace, basic]

    refs = SkillCatalog(app=app, session_id=sid)._catalog_refs()
    bodies = {ref.id: ref.body_provider() for ref in refs}

    workspace = bodies["a2ui-catalog-clio-workspace"]
    basic = bodies["a2ui-catalog-basic"]
    assert f"Catalog id: `{WORKSPACE_ID}`" in workspace
    assert "This is this agent's default catalog" in workspace
    assert f"Catalog id: `{BASIC_ID}`" in basic
    assert "This is not this agent's default catalog" in basic
    assert f'catalog_id="{BASIC_ID}"' in basic


# --------------------------------------------------------------------------- #
# A session whose agent does not resolve is not "declares nothing"            #
# --------------------------------------------------------------------------- #


def test_unresolved_session_agent_reports_its_own_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid = _app_session(tmp_path)
    app.state.sessions.update(
        sid, metadata_patch={"agent_ref": "clio/gone-agent", "agent_resolution": ""}
    )
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    _advertise(app, sid, [WORKSPACE_ID])

    assert session_producible_catalog_ids(app, sid) == []
    assert resolve_session_catalogs(app, sid).empty_reason() == "a2ui_agent_unresolved"
    assert select_catalog(app, sid, record=False).reason == "a2ui_agent_unresolved"
    assert session_a2ui_producers_enabled(app, sid) is False
    result = build_create_a2ui_surface_tool()(
        surface_id="s", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    assert result["ok"] is False
    assert result["reason"] == "a2ui_agent_unresolved"
    assert result["hint"]
    assert "do not retry" not in result["hint"]
    assert "a2ui_agent_unresolved" in _reasons(app, sid)
