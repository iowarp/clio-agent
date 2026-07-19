"""Parity tests for the unified agent-resolution seam (#770 Wave-C C1).

``GET /v1/agents`` (the route path, backed by ``_agent_rows``) and the actual
turn engine (the ``clio_agent.gact.agents.resolution`` module stack) MUST compute
the SAME effective agent rows for a given session: identical MCP tool-gating,
identical default-blueprint fallback, identical capability refs. Before the C1
unification these were two independent stacks that disagreed; these tests lock
the single-seam invariant so the divergence cannot come back.
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
    # The default-blueprint fallback is the behaviour under test. (#948 S4b retired
    # the legacy-native-experts short-circuit that used to suppress it.)
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


def test_default_blueprint_fallback_parity(tmp_path: Path) -> None:
    """#1: a session with no explicit blueprint but a discoverable DEFAULT one.

    The route (`/v1/agents`) and the runtime resolver must agree on the effective
    agent id-set. Pre-fix the route saw an empty blueprint set and fell back to
    the builtin/expert-pack hierarchy while the runtime executed the default
    blueprint's experts -- two different agent sets for the same session.
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
            json={"title": "fallback", "workspace_id": wid},
        ).json()["id"]

        route_ids = _route_enabled_ids(client, sid, wid)
        runtime_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)

    # The runtime resolves the default blueprint's experts (no explicit activation).
    assert runtime_ids == {"root", "variant"}
    # The route MUST resolve the exact same effective set.
    assert route_ids == runtime_ids


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
    it), and for a default-fallback session the runtime-selected root expert must be
    one of the agents ``/v1/agents`` lists.
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


def test_no_resolvable_agent_fails_typed_and_never_runs_forward(tmp_path: Path) -> None:
    """#948 S4b: a default/main session with NO resolvable Agent Blueprint must fail
    with a typed ``no_resolvable_agent`` error and must NEVER fall through to the
    deleted legacy planner ``forward``.

    The autouse ``_isolate_config`` fixture drops the legacy-native-experts flag and
    disables the default-registry bootstrap, and this session has no workspace
    blueprint — so nothing resolves, which is exactly the else-branch condition
    turn_forward now converts into a typed failure.

    Sabotage check: restore the deleted else branch (dispatch to
    ``app.state.agent.forward(...)`` instead of raising ``_NoResolvableAgent``) and
    this goes red on BOTH the missing typed error and the non-zero spy ``forward``
    count.
    """

    spy = _ForwardSpyAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=spy)
    with TestClient(app) as client:
        from .conftest import complete_turn

        sid = client.post("/v1/sessions", json={"title": "no-blueprint"}).json()["id"]
        assistant = complete_turn(client, sid, "do something")

    error_info = assistant.get("error_info") or {}
    assert error_info.get("error") == "no_resolvable_agent"
    recovery = (error_info.get("details") or {}).get("recovery_actions", [])
    assert "install_default_registry" in recovery
    assert "activate_agent_blueprint" in recovery
    # The deleted legacy planner dispatch must never have been reached.
    assert spy.forward_calls == 0


class _RecordingHostAgent:
    """Host fake whose ``forward`` proves the turn ran the blueprint branch.

    Under the ``host_agent_executor`` seam the default-registry react main
    delegates to this ``forward`` instead of an LM-bound DSPy program, so a
    recorded question is positive proof the blueprint branch executed the turn.
    """

    def __init__(self) -> None:
        self.questions: list[str] = []

    def forward(self, question: str, session_id: str, **_kw: object) -> object:
        self.questions.append(question)
        return type(
            "P",
            (),
            {"answer": f"handled: {question}", "selected_expert": "", "routing_rationale": ""},
        )()


def _write_loose_workspace_expert(workspace: Path) -> None:
    """Drop a DISCOVERABLE (un-activated) loose workspace expert under ``.clio``.

    Its mere presence makes ``discover_expert_packs(cwd=workspace)`` non-empty (the
    pack is id ``workspace.experts``, a LOOSE pack with ``manifest_path is None``).
    A loose expert must NOT suppress the default (only a workspace MANIFEST pack or
    an explicit session activation does), so it stays available as a delegate of the
    resolved default main rather than shadowing it.
    """

    loose = workspace / ".clio" / "experts"
    loose.mkdir(parents=True, exist_ok=True)
    loose.joinpath("helper.md").write_text(
        """---
id: helper
title: Helper
tier: 2
---
Help out.
""",
        encoding="utf-8",
    )


def _write_global_manifest_pack(home: Path) -> None:
    """Install a GLOBAL manifest expert-pack under the isolated per-user config.

    ``discover_expert_packs`` reports this as a ``scope == "global"`` pack with
    ``manifest_path`` set. Finding #948-S4b-[4]: a global/marketplace pack must NOT
    suppress the default (only a WORKSPACE manifest pack does), so the
    ``pack.scope == "workspace"`` filter in the suppression predicate is what keeps
    this from shadowing the default main.
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
parent_id: main
tier: 2
---
Help globally.
""",
        encoding="utf-8",
    )


def test_discoverable_pack_does_not_suppress_default_blueprint(
    tmp_path: Path, host_agent_executor
) -> None:
    """A discoverable LOOSE expert / GLOBAL pack must NOT suppress the default.

    Owner decision (#948 S4b review): the precedence guard suppresses the default
    react main ONLY for an EXPLICIT activation -- a session-activated pack or a
    WORKSPACE manifest expert-pack. Two configurations that must NOT suppress and
    are covered here together (the common "default registry + some experts on disk"
    deployment): a loose workspace expert (``.clio/experts/helper.md``) and a global
    marketplace-style manifest pack (``~/.config/.../expert-packs/*/clio-pack.yaml``).
    Both must leave the default blueprint resolving (they become delegates of the
    resolved main), or a plain default session would fail ``no_resolvable_agent``.

    Sabotage (two independent guards, each turns this red):
      * broaden the predicate to ``or discover_expert_packs(cwd=pack_cwd)`` (drop the
        ``manifest_path is not None`` filter) -> the loose expert suppresses; or
      * drop the ``pack.scope == "workspace"`` filter -> the global manifest pack
        suppresses.
    Either way the blueprint id resolves to "" -> ``runtime_ids`` empties and the
    turn fails ``no_resolvable_agent`` -> the id-set + ``agent_runtime`` assertions go red.
    """

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_simple_blueprint(blueprint, DEFAULT_AGENT_BLUEPRINT_ID, variant_tools=["noop_tool"])
    _write_loose_workspace_expert(workspace)
    # ``_isolate_config`` points the per-user config root at ``tmp_path/user-config``
    # (via ``CLIO_USER_DIR``), so the global pack lands in the isolated root scanned
    # by ``discover_expert_packs`` (config_root/expert-packs), not the real machine.
    _write_global_manifest_pack(tmp_path)

    host = _RecordingHostAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=host)
    with TestClient(app) as client:
        from .conftest import complete_turn

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

        # The discoverable pack does NOT shadow the default blueprint: it still
        # resolves its experts for this un-activated default session.
        runtime_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)
        assert runtime_ids == {"root", "variant"}

        # And a bare turn RUNS through the blueprint branch (default react main),
        # not the deleted legacy planner and not the no_resolvable_agent else.
        assistant = complete_turn(client, sid, "do the thing")

    assert (assistant.get("error_info") or {}).get("error") != "no_resolvable_agent"
    # Positive proof the default-registry main executed the turn (finding #8's
    # default-main provenance, asserted on the wire).
    assert assistant["metadata"]["agent_runtime"]["agent_id"] == "root"
    assert host.questions == ["do the thing"]


def test_activated_pack_suppresses_implicit_default_blueprint(tmp_path: Path) -> None:
    """An EXPLICITLY activated expert pack still suppresses the implicit default.

    This is the behaviour the precedence guard exists for: activating a pack is an
    explicit user choice, so the default-registry blueprint must not shadow the
    pack's experts (#770 C1 list==execute). Contrast with
    ``test_discoverable_pack_does_not_suppress_default_blueprint``: mere
    discoverability does NOT suppress; explicit activation DOES.

    Sabotage: delete the two activation clauses in
    ``_runtime_active_agent_blueprint_id`` -> the blueprint id stays
    ``DEFAULT_AGENT_BLUEPRINT_ID`` even after activation -> the post-activation
    assertion goes red.
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

        # Before activation: the discoverable pack does NOT suppress the default.
        assert (
            _resolution._runtime_active_agent_blueprint_id(app, sid) == DEFAULT_AGENT_BLUEPRINT_ID
        )

        # Explicitly activate the discoverable loose-experts pack (workspace.experts).
        resp = client.post(
            f"/v1/sessions/{sid}/expert-pack",
            json={"pack_id": "workspace.experts"},
        )
        assert resp.status_code == 200, resp.text

        # After explicit activation: the implicit default IS suppressed (the pack's
        # experts become the session's agent set via ``_agent_rows``).
        assert _resolution._runtime_active_agent_blueprint_id(app, sid) == ""
