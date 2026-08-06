"""Parity tests for the unified agent-resolution seam (#770 Wave-C C1).

``GET /v1/agents`` (the route path, backed by ``_agent_rows``) and the actual
turn engine (the ``clio_agent.gact.agents.resolution`` module stack) MUST compute
the SAME effective agent rows for a given session: identical MCP tool-gating,
identical blueprint-activation semantics, identical capability refs. Before the
C1 unification these were two independent stacks that disagreed; these tests
lock the single-seam invariant so the divergence cannot come back.

Blueprint activation is EXPLICIT only (owner ruling, 2026-08-05): a session that
activated no blueprint resolves NO blueprint. The implicit fallback to a
discoverable ``DEFAULT_AGENT_BLUEPRINT_ID`` is DELETED from
``_runtime_active_agent_blueprint_id`` — a bare session must never silently
inherit an expert hierarchy it never asked for. Activation happens only via
``POST /v1/sessions/{sid}/agent-blueprint`` or explicit session metadata.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID
from clio_agent.gact.agents import resolution as _resolution
from clio_agent.gact.agents.resolution import (
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_root_id,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the per-user config dir + disable default-registry bootstrap.

    Keeps the machine's real installed marketplace (which ships its own
    ``earthscope-gnss-region`` default blueprint) out of the resolved set, so the
    only ``DEFAULT_AGENT_BLUEPRINT_ID`` blueprint discovered is the clean two-expert
    one a test writes into its workspace.
    """

    monkeypatch.setenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", "1")
    # Explicit-activation-only resolution is the behaviour under test: the implicit
    # default-blueprint fallback is DELETED (owner ruling 2026-08-05).
    # ``CLIO_USER_DIR`` wins over ``XDG_CONFIG_HOME`` (paths.user_config_dir_for),
    # so an empty per-user root keeps both the machine's real marketplace and the
    # conftest's ambient XDG default blueprint out of the resolved set.
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _write_simple_blueprint(root: Path, blueprint_id: str, *, variant_tools: list[str]) -> None:
    """Write a minimal two-expert (root -> variant) blueprint to ``root``."""

    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: {blueprint_id.title()} Agent
root_expert: root
---
Test blueprint.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Root
tier: 1
module:
  kind: react
---
Coordinate.
""",
        encoding="utf-8",
    )
    tools_block = "\n".join(f"  - {name}" for name in variant_tools)
    root.joinpath("experts", "variant.md").write_text(
        f"""---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
{tools_block}
---
Do variant work.
""",
        encoding="utf-8",
    )


def _route_enabled_ids(client: TestClient, sid: str, wid: str = "") -> set[str]:
    params = {"session_id": sid}
    if wid:
        params["workspace_id"] = wid
    return {
        row["id"]
        for row in client.get("/v1/agents", params=params).json()["agents"]
        if row.get("enabled", True)
    }


def _route_row(client: TestClient, sid: str, agent_id: str, wid: str = "") -> dict | None:
    params = {"session_id": sid}
    if wid:
        params["workspace_id"] = wid
    for row in client.get("/v1/agents", params=params).json()["agents"]:
        if row["id"] == agent_id:
            return row
    return None


def test_unbound_session_resolves_no_blueprint_and_parity_holds(tmp_path: Path) -> None:
    """#1: a session with NO activated blueprint resolves NO blueprint experts.

    Owner ruling (2026-08-05): a session that activated nothing gets no
    blueprint — the implicit fallback to a discoverable
    ``DEFAULT_AGENT_BLUEPRINT_ID`` is deleted, so a bare session must never
    silently inherit an expert hierarchy it never asked for. The route
    (`/v1/agents`) and the runtime resolver must agree on that emptiness, and
    after an EXPLICIT ``POST /v1/sessions/{sid}/agent-blueprint`` activation
    they must agree on the blueprint's expert set — parity in both states.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "unbound", "workspace_id": wid},
        ).json()["id"]

        # Unbound: the discoverable default blueprint is NOT implicitly resolved.
        unbound_runtime_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)
        unbound_route_ids = _route_enabled_ids(client, sid, wid)

        # Explicit activation is the ONE way to bind the blueprint to the session.
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": DEFAULT_AGENT_BLUEPRINT_ID},
            ).status_code
            == 200
        )

        activated_runtime_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)
        activated_route_ids = _route_enabled_ids(client, sid, wid)

    # No activation -> no blueprint experts, on the runtime blueprint-lane seam.
    assert unbound_runtime_ids == set()
    # The route's own emptiness-parity is scoped to the BLUEPRINT lane only: the
    # route additionally reports the in-code builtin main (catalog._builtin_agents
    # -> _builtin_main_agent, no accretion of a discoverable-but-unactivated
    # registry snapshot) -- the SAME agent a bare turn actually executes
    # (turn_forward.run_builtin_main). It is the one honest non-blueprint entry a
    # bare session's route always carries.
    assert unbound_route_ids == {"main"}
    # Explicit activation -> the blueprint's experts, identical on both seams.
    assert activated_runtime_ids == {"root", "variant"}
    assert activated_route_ids == activated_runtime_ids


def test_mcp_tool_gating_parity(tmp_path: Path) -> None:
    """#2: an expert whose declared MCP tool needs (absent) explicit enablement.

    The route disables the expert via descriptor validation; the runtime resolver
    must disable it identically. Pre-fix the runtime never applied MCP gating, so
    it executed an expert the UI reported as unavailable.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_simple_blueprint(blueprint, "earth", variant_tools=["earthscope_query"])
    (blueprint / "tools").mkdir()
    blueprint.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "earth", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": "earth"},
            ).status_code
            == 200
        )

        route_variant = _route_row(client, sid, "variant", wid)
        assert route_variant is not None
        route_enabled = bool(route_variant["enabled"])

        runtime_variant = _resolve_runtime_dynamic_agent(
            app, "variant", session_id=sid, workspace_id=wid
        )
        runtime_enabled = runtime_variant is not None and runtime_variant.enabled

    # The MCP tool is declared but never enabled: BOTH entry points must gate it off.
    assert route_enabled is False
    assert route_enabled == runtime_enabled


def test_single_resolution_seam(tmp_path: Path, monkeypatch) -> None:
    """#3: both entry points must dispatch through the ONE resolution function.

    Patching ``resolution._runtime_active_agent_blueprint_rows`` to a sentinel must
    be observed by BOTH ``GET /v1/agents`` and ``_resolve_runtime_dynamic_agent``.
    Pre-fix the route went through an independent ``build_app`` closure that never
    saw the patch.
    """

    sentinel = AgentDef(id="sentinel", source="expert_pack", title="Sentinel", enabled=True)

    def _patched(app, *, session_id="", workspace_id="", prompt_registry=None):
        return [sentinel] if session_id else []

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    monkeypatch.setattr(_resolution, "_runtime_active_agent_blueprint_rows", _patched)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "seam"}).json()["id"]

        route_ids = {
            row["id"]
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }
        runtime_row = _resolve_runtime_dynamic_agent(app, "sentinel", session_id=sid)

    assert "sentinel" in route_ids
    assert runtime_row is not None and runtime_row.id == "sentinel"


def test_import_seam_and_root_id_symmetry(tmp_path: Path) -> None:
    """#4: the app.py re-export stays importable and root-id agrees with the list.

    The module-level ``from clio_agent.gact.app import _resolve_runtime_dynamic_agent``
    re-export must survive the refactor (turn.py + the import-seam guard depend on
    it), and for a session with an EXPLICITLY activated blueprint (the implicit
    default fallback is deleted) the runtime-selected root expert must be one of
    the agents ``/v1/agents`` lists.
    """

    from clio_agent.gact.app import (  # noqa: PLC0415
        _resolve_runtime_dynamic_agent as _reexported,
    )

    assert _reexported is not None

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "root-sym", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": DEFAULT_AGENT_BLUEPRINT_ID},
            ).status_code
            == 200
        )

        route_ids = _route_enabled_ids(client, sid, wid)
        root_id = _runtime_active_agent_blueprint_root_id(app, sid)

    assert root_id == "root"
    assert root_id in route_ids


class _ForwardSpyAgent:
    """Stand-in main agent whose legacy ``forward`` must NEVER be invoked (#948 S4b).

    The Tier-1 planner ``ClioAgent.forward`` is deleted; this spy exists only to
    prove the turn engine never falls back to a legacy ``app.state.agent.forward``
    dispatch when no Agent Blueprint resolves.
    """

    def __init__(self) -> None:
        self.forward_calls = 0

    def forward(self, *_args: object, **_kwargs: object) -> object:  # pragma: no cover
        self.forward_calls += 1
        raise AssertionError("legacy planner forward must not run (#948 S4b)")


def test_bare_session_runs_builtin_main_and_fails_typed_on_inexecutable_host(
    tmp_path: Path,
) -> None:
    """A BARE session resolves the in-code builtin react ``main`` — never nothing.

    Owner ruling (2026-08-05) + RULE 2: a session that activated no blueprint
    must never inherit a discoverable one, but it must still WORK — it executes
    the shipped builtin main (``catalog._builtin_main_agent``) on the react
    runtime. This host fake carries no tool executor, so the runtime genuinely
    cannot execute the builtin main's declared native tool surface: the turn
    must fail TYPED at module build (``not_implemented`` /
    ``custom_agent_tool_executor_unavailable``) — a structured error turn, never
    an EMPTY assistant message, and NEVER the deleted legacy planner
    ``forward`` (spy count stays 0).

    Sabotage checks: restore the pre-fix else branch (raise
    ``_NoResolvableAgent`` for bare sessions) and the error flips back to
    ``no_resolvable_agent``; restore the deleted legacy dispatch
    (``app.state.agent.forward(...)``) and the spy count goes non-zero.
    """

    spy = _ForwardSpyAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=spy)
    with TestClient(app) as client:
        from .conftest import complete_turn

        sid = client.post("/v1/sessions", json={"title": "no-blueprint"}).json()["id"]
        assistant = complete_turn(client, sid, "do something")

    error_info = assistant.get("error_info") or {}
    assert error_info.get("error") == "not_implemented"
    details = error_info.get("details") or {}
    # The BUILTIN main resolved (never nothing, never a discoverable blueprint);
    # what failed is this host's ability to execute its native tool surface.
    assert details.get("agent_id") == "main"
    assert details.get("reason") == "custom_agent_tool_executor_unavailable"
    assert details.get("unsupported_tools"), "builtin main must declare its native tool surface"
    # The deleted legacy planner dispatch must never have been reached.
    assert spy.forward_calls == 0


def test_activated_blueprint_resolving_nothing_fails_no_resolvable_agent(
    tmp_path: Path,
) -> None:
    """An EXPLICITLY activated blueprint that resolves nothing stays TYPED.

    The builtin main is for BARE sessions only: a session whose metadata names
    an activated blueprint (here one missing from disk) must not be silently
    downgraded to the builtin main — that would mask a broken activation. It
    keeps the ``no_resolvable_agent`` envelope, and the deleted legacy planner
    ``forward`` is still never reached.
    """

    spy = _ForwardSpyAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=spy)
    with TestClient(app) as client:
        from .conftest import complete_turn

        sid = client.post("/v1/sessions", json={"title": "ghost-activation"}).json()["id"]
        app.state.sessions.update(
            sid, metadata_patch={"active_agent_blueprint_id": "ghost-blueprint"}
        )
        assistant = complete_turn(client, sid, "do something")

    error_info = assistant.get("error_info") or {}
    assert error_info.get("error") == "no_resolvable_agent"
    recovery = (error_info.get("details") or {}).get("recovery_actions", [])
    assert "install_default_registry" in recovery
    assert "activate_agent_blueprint" in recovery
    # The deleted legacy planner dispatch must never have been reached.
    assert spy.forward_calls == 0


def _write_loose_workspace_expert(workspace: Path) -> None:
    """Drop a DISCOVERABLE (un-activated) loose workspace expert under ``.clio``.

    Its mere presence makes ``discover_expert_packs(cwd=workspace)`` non-empty (the
    pack is id ``workspace.experts``, a LOOSE pack with ``manifest_path is None``).
    Discoverability alone never resolves a blueprint; the loose expert is simply
    part of the expert-pack/builtin hierarchy the route serves for an unbound
    session. Tier 1 with no parent: standalone-valid, because there is no
    implicit default "main" left for it to hang off (tier > 1 without a
    parent_id fails hierarchy validation and would be served disabled).
    """

    loose = workspace / ".clio" / "experts"
    loose.mkdir(parents=True, exist_ok=True)
    loose.joinpath("helper.md").write_text(
        """---
id: helper
title: Helper
tier: 1
---
Help out.
""",
        encoding="utf-8",
    )


def _write_global_manifest_pack(home: Path) -> None:
    """Install a GLOBAL manifest expert-pack under the isolated per-user config.

    ``discover_expert_packs`` reports this as a ``scope == "global"`` pack with
    ``manifest_path`` set. Like the loose workspace expert, its discoverability
    never resolves a blueprint — it only joins the expert-pack/builtin hierarchy
    the route serves when no blueprint is activated. Tier 1 with no parent:
    there is no implicit default "main" left to declare as ``parent_id`` (an
    unresolvable parent fails hierarchy validation and is served disabled).
    """

    from clio_agent import paths

    config_root = paths.user_config_dir_for(home, os.environ)
    pack = config_root / "expert-packs" / "global-helpers"
    (pack / "experts").mkdir(parents=True, exist_ok=True)
    pack.joinpath("clio-pack.yaml").write_text(
        "id: global-helpers\nversion: 0.1.0\ntitle: Global Helpers\n",
        encoding="utf-8",
    )
    pack.joinpath("experts", "ghelper.md").write_text(
        """---
id: ghelper
title: Global Helper
tier: 1
---
Help globally.
""",
        encoding="utf-8",
    )


def test_discoverable_pack_and_default_blueprint_resolve_no_blueprint(tmp_path: Path) -> None:
    """Discoverability is NOT activation: nothing on disk resolves implicitly.

    The predecessor of this test pinned "a discoverable pack must not suppress
    the implicit default blueprint". That whole precedence question is deleted
    with the implicit fallback (owner ruling 2026-08-05): with a discoverable
    DEFAULT blueprint, a loose workspace expert (``.clio/experts/helper.md``)
    AND a global manifest expert-pack all on disk but NOTHING activated, the
    session resolves NO blueprint at all — there is no implicit default left to
    suppress or protect. What the route honestly serves for such an unbound
    session is the expert-pack/builtin hierarchy: the code-shipped builtin
    ``main`` (``catalog._builtin_agents`` no longer smuggles in the discoverable
    default registry snapshot either, closing the parallel implicit-selection
    seam the blueprint-lane ruling left open) plus the discoverable loose
    workspace expert and the global pack's expert.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])
    _write_loose_workspace_expert(workspace)
    # ``_isolate_config`` points the per-user config root at ``tmp_path/user-config``
    # (via ``CLIO_USER_DIR``), so the global pack lands in the isolated root scanned
    # by ``discover_expert_packs`` (config_root/expert-packs), not the real machine.
    _write_global_manifest_pack(tmp_path)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "discoverable-pack", "workspace_id": wid},
        ).json()["id"]

        # No activation -> no blueprint, regardless of what is discoverable.
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""
        assert _runtime_active_agent_blueprint_agent_ids(app, sid) == set()

        # The route serves the expert-pack/builtin hierarchy for the unbound
        # session: the loose workspace expert and the global pack's expert, and
        # NOT the un-activated blueprint's experts.
        route_ids = _route_enabled_ids(client, sid, wid)

    assert route_ids == {"main", "helper", "ghelper"}


def test_activated_pack_without_blueprint_activation_resolves_no_blueprint(
    tmp_path: Path,
) -> None:
    """An activated expert pack binds the PACK — a blueprint still never appears.

    The predecessor of this test pinned "explicit pack activation suppresses the
    implicit default blueprint". The suppression mechanism is deleted with the
    implicit fallback itself (owner ruling 2026-08-05): the blueprint id is ""
    before AND after pack activation, because only blueprint activation ever
    binds a blueprint. What pack activation does control is the effective agent
    set the route serves: the activated pack's experts.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])
    _write_loose_workspace_expert(workspace)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "activated-pack", "workspace_id": wid},
        ).json()["id"]

        # Before pack activation: no blueprint (nothing was activated).
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""

        # Explicitly activate the discoverable loose-experts pack (workspace.experts).
        resp = client.post(
            f"/v1/sessions/{sid}/expert-pack",
            json={"pack_id": "workspace.experts"},
        )
        assert resp.status_code == 200, resp.text

        # After pack activation: STILL no blueprint — pack activation binds the
        # pack, never a blueprint, and the deleted implicit default stays gone.
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""
        assert _runtime_active_agent_blueprint_agent_ids(app, sid) == set()

        # The activated pack's experts are the effective agent set the route serves,
        # alongside the code-shipped builtin ``main`` that is always present.
        route_ids = _route_enabled_ids(client, sid, wid)

    assert route_ids == {"main", "helper"}


def test_discoverable_default_blueprint_never_implicitly_activates(tmp_path: Path) -> None:
    """REGRESSION PIN (owner ruling 2026-08-05): discoverable is NOT activated.

    The regression the owner hit: a bare session in a workspace where
    ``DEFAULT_AGENT_BLUEPRINT_ID`` ("earthscope-gnss-region") was merely
    discoverable silently inherited the blueprint's full expert hierarchy — the
    session never asked for it. The implicit discovery fallback is deleted from
    ``_runtime_active_agent_blueprint_id``; this test keeps it deleted.

    Sabotage: restore the fallback (return a discovered
    ``DEFAULT_AGENT_BLUEPRINT_ID`` when the session metadata names no blueprint)
    -> the resolved id becomes the default blueprint's id and this goes red.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "bare", "workspace_id": wid},
        ).json()["id"]

        # The default blueprint IS discoverable for this workspace...
        cwd = _resolution._runtime_workspace_catalog_cwd(app, session_id=sid)
        from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

        assert any(
            row.id == DEFAULT_AGENT_BLUEPRINT_ID for row in discover_agent_blueprints(cwd=cwd)
        )

        # ...and it must STILL never resolve without explicit activation.
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""


def test_installed_default_registry_never_implicitly_selected_by_builtin_catalog(
    tmp_path: Path,
) -> None:
    """REGRESSION PIN (live defect, session sess_4c373a7185ca, 2026-08-06).

    aa906022 deleted the implicit fallback from
    ``_runtime_active_agent_blueprint_id`` (the BLUEPRINT lane) but left a SECOND,
    parallel implicit-selection seam alive: ``catalog._builtin_agents()``
    unconditionally loaded whatever Agent Blueprint snapshot was pinned as
    ``DEFAULT_AGENT_BLUEPRINT_ID`` and relabeled its rows "builtin" -- so a bare
    session on a box where the default registry IS actually installed (the
    live/production case: ``earthscope-gnss-region`` under
    ``<user-config>/agent-blueprints/``) still silently inherited its full
    expert hierarchy via ``GET /v1/agents`` and ``_resolve_runtime_dynamic_agent``,
    even though the blueprint lane correctly reported no active blueprint. This
    reproduces the live defect: a session with
    ``metadata.active_agent_blueprint_id`` unset resolved to the installed
    pack's root with ``source: expert_pack`` instead of the code-shipped
    builtin main.

    Sabotage: restore ``_builtin_agents()``'s old body (``load_agent_blueprints``
    filtered by ``DEFAULT_AGENT_BLUEPRINT_ID``, tagging ``source_blueprint``) ->
    ``route_ids`` gains ``"root"``/``"variant"`` and ``runtime_main.source``
    flips to ``"expert_pack"``; this goes red.
    """

    from clio_agent import paths

    config_root = paths.user_config_dir_for(tmp_path, os.environ)
    installed = config_root / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(installed, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "bare-no-workspace"}).json()["id"]

        # The blueprint lane correctly reports no active blueprint...
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""
        assert _runtime_active_agent_blueprint_agent_ids(app, sid) == set()

        # ...and the expert-pack/builtin fallback must resolve ONLY the
        # code-shipped main -- never the installed-but-unactivated snapshot.
        route_ids = _route_enabled_ids(client, sid)
        runtime_main = _resolve_runtime_dynamic_agent(app, "main", session_id=sid)

    assert route_ids == {"main"}
    assert runtime_main is not None
    assert runtime_main.source == "builtin"
    assert runtime_main.metadata.get("definition_kind") == "builtin_main"


def test_bare_main_never_inherits_installed_default_registry_children(
    tmp_path: Path,
) -> None:
    """REGRESSION PIN: a bare session's ``main`` gains no declared children from
    an installed-but-unactivated default registry snapshot.

    Mirrors the live defect exactly: the real installed default registry
    (``earthscope-gnss-region``) declares its OWN root expert with id ``main``
    plus children (``geospatial``, ``data``, ...). Before this fix,
    ``_runtime_child_agent_rows`` (and its detached-execution twin
    ``spawn_context.declared_child_ids_from_bindings``) merged
    ``catalog._builtin_agents()`` -- which silently loaded that installed
    snapshot -- with genuinely-installed packs, so a BARE session's
    code-shipped ``main`` (the agent the turn actually executes,
    ``catalog._builtin_main_agent``) would see the pack's children as if IT had
    declared them, and the spawn-runtime tools would let it spawn them. A bare
    session must see NO declared children for the builtin main unless a
    workspace/global expert genuinely declares ``parent_id: main``.
    """

    from clio_agent import paths
    from clio_agent.gact.agents.resolution import _runtime_child_agent_rows

    config_root = paths.user_config_dir_for(tmp_path, os.environ)
    installed = config_root / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    (installed / "experts").mkdir(parents=True)
    installed.joinpath("AGENT.md").write_text(
        f"""---
id: {DEFAULT_AGENT_BLUEPRINT_ID}
version: 0.1.0
title: Installed Default
root_expert: main
---
Installed default registry blueprint.
""",
        encoding="utf-8",
    )
    installed.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Installed Main
tier: 1
module:
  kind: react
---
Coordinate installed-pack work.
""",
        encoding="utf-8",
    )
    installed.joinpath("experts", "geospatial.md").write_text(
        """---
id: geospatial
title: Installed Geospatial
parent_id: main
tier: 2
tools:
  - noop_tool
---
Resolve geography.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "bare-children"}).json()["id"]

        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""
        children = _runtime_child_agent_rows(app, "main", session_id=sid)

    assert children == []
