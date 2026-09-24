"""EarthScope pack A2UI catalog composability proof (S7).

docs/design/a2ui-compat-campaign-2026-09.md S7 / iowarp/clio-agent-marketplace
issue #69 deliverable 4 (the clio-agent half): the campaign's composability
proof runs against an UNMODIFIED clio-agent server -- no ``src/`` edit in this
slice. A marketplace pack, ``external/clio-agent-marketplace/
earthscope-single-agent/``, ships its OWN A2UI catalog
(``catalogs/earthscope-stations/{catalog.json, catalog.clio.json,
instructions.md}``), declared in ``AGENT.md``'s ``a2ui_catalogs:`` mapping
exactly the way an MCP server is declared. This installs that REAL pack (the
submodule, pinned to the S7 catalog commit -- never a fixture stand-in) into
an isolated user config dir, activates its blueprint in a session, and proves
every S2-S5 seam accepts it: catalog discovery, server-wide and per-client
capability negotiation, the producer tool with the worked example parsed
straight out of ``instructions.md`` (so this test cannot drift from the
docs), the event dispatcher's idle/duplicate/invalid-context paths, the
generated catalog skill, and a ``waiting_user`` resume.

Every assertion reads the STORED/served value (the app's own
``a2ui_store``/``messages``/``user_questions`` state, or the HTTP response
body), never a client-side echo.

Harness reused from ``tests/test_gact/test_a2ui_catalog_registry.py``'s
``_install_isolated_minimal_pack`` (adversarial S7 finding #13: a bare
``Path.home`` monkeypatch does not redirect the per-user blueprint registry
root on Windows) and ``tests/test_gact/test_a2ui_actions.py``'s
``_pending_question``/``_stub_spawn`` waiting_user/idempotency harness.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient

from clio_agent import paths
from clio_agent.gact import agent_blueprint_requires
from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities, select_catalog
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.agent_blueprints import install_agent_blueprint
from clio_agent.gact.agents.skill_runtime import build_load_skill_tool, skill_runtime_for_agent
from clio_agent.gact.app import build_app
from clio_agent.gact.interaction_types import UserQuestion
from clio_agent.gact.types import AgentDef

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}

PACK_ROOT = (
    Path(__file__).resolve().parents[2]
    / "external"
    / "clio-agent-marketplace"
    / "earthscope-single-agent"
)
PACK_BLUEPRINT_ID = "earthscope-single-agent"
PACK_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/earthscope-stations/v1"
BASIC_CATALOG_ID = basic_catalog_id()
WORKSPACE_CATALOG_ID = workspace_catalog_id()
CATALOG_SKILL_ID = "a2ui-catalog-earthscope-stations"

_INSTRUCTIONS_PATH = PACK_ROOT / "catalogs" / "earthscope-stations" / "instructions.md"


# --------------------------------------------------------------------------- #
# Install + activate + capability-advertisement harness                      #
# --------------------------------------------------------------------------- #


def _pack_floor_version() -> str:
    """The lowest clio-agent version the real pack's floor admits."""

    from packaging.specifiers import SpecifierSet

    from clio_agent import __version__
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root

    requires = parse_agent_blueprint_root(PACK_ROOT, scope="session").metadata.get("requires")
    floor = str((requires or {}).get("clio_agent") or "")
    minimums = [spec.version for spec in SpecifierSet(floor) if spec.operator in {">=", "=="}]
    return minimums[0] if minimums else __version__


def _install_isolated_earthscope_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, Any]]:
    """Install the REAL EarthScope pack into a tmp-isolated user config dir.

    Mirrors ``test_a2ui_catalog_registry.py``'s ``_install_isolated_minimal_
    pack``: ``install_agent_blueprint``'s install root resolves through
    ``paths.user_config_dir_for(home, os.environ)``, which checks
    ``CLIO_USER_DIR``/``XDG_CONFIG_HOME`` in ``os.environ`` BEFORE ever
    consulting the injected ``home`` argument -- a bare ``Path.home``
    monkeypatch does NOT redirect it on Windows. Both env vars are set
    explicitly (belt-and-suspenders, not an implicit dependency on the
    autouse ``allow_pytest_tmp_path`` fixture elsewhere), and the real
    per-user registry is asserted untouched after install.
    """

    real_root = paths.user_config_dir_for(Path.home(), dict(os.environ))

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # The pack's ``requires.clio_agent`` floor names the release that ships this
    # runtime, so a pre-release checkout is below it by construction. These tests
    # exercise catalog composability, not the floor (test_agent_blueprints.py
    # covers the floor), so run at EXACTLY the pack's declared minimum.
    floor_version = _pack_floor_version()
    monkeypatch.setattr(
        agent_blueprint_requires, "_running_clio_agent_version", lambda: floor_version
    )

    result = install_agent_blueprint(
        source=str(PACK_ROOT),
        scope="global",
        cwd=cwd,
        home=home,
        blueprint_id=PACK_BLUEPRINT_ID,
    )

    leaked = real_root / "agent-blueprints" / PACK_BLUEPRINT_ID
    assert not leaked.exists(), f"real pack leaked into the real registry: {leaked}"
    return home, cwd, result


def _activated_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Any, str]:
    """Install the real pack and activate its blueprint in a fresh session."""

    _install_isolated_earthscope_pack(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="EarthScope pack")
    app.state.sessions.update(
        session.id, metadata_patch={"active_agent_blueprint_id": PACK_BLUEPRINT_ID}
    )
    return TestClient(app), app, session.id


def _advertise_pack_first(app: Any, session_id: str) -> None:
    """Advertise client capabilities listing the pack catalog first.

    The client also renders clio-workspace and Basic, as a real client does.
    """

    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": [PACK_CATALOG_ID, WORKSPACE_CATALOG_ID, BASIC_CATALOG_ID]}}
    )
    remember_client_capabilities(app, session_id, caps)


def _stub_spawn(app: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Prevent a real turn from executing; return the list of spawned coros."""

    spawned: list[Any] = []

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(app.state.turn_runner, "spawn", _spawn)
    return spawned


# --------------------------------------------------------------------------- #
# The worked example, parsed out of instructions.md (never hand-copied)      #
# --------------------------------------------------------------------------- #


def _instructions_text() -> str:
    return _INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def _parse_worked_example_block(text: str, label: str) -> dict[str, Any]:
    """Parse one named ```json code block out of the worked example.

    Regex-extracted from the pack's own docs -- never hand-copied -- so this
    test cannot silently drift from ``instructions.md``.
    """

    pattern = rf"`{re.escape(label)}`:\s*\n\n```json\n(.*?)\n```"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"instructions.md has no {label!r} worked-example block"
    return json.loads(match.group(1))


def _create_example_surface(
    app: Any, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], str]:
    """Create the pack's worked-example surface through the real producer tool.

    Every field comes from ``instructions.md``'s own worked example, parsed
    at test time -- never hand-typed.
    """

    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session_id)

    text = _instructions_text()
    create_block = _parse_worked_example_block(text, "createSurface")
    update_components = _parse_worked_example_block(text, "updateComponents")
    update_data_model = _parse_worked_example_block(text, "updateDataModel")
    surface_id = str(create_block["createSurface"]["surfaceId"])
    components = update_components["updateComponents"]["components"]
    component_names = {c["component"] for c in components}
    # Guard the parse itself: a future docs edit that drops these components
    # must fail THIS assertion loudly, not silently pass on an empty set.
    assert {"StationMap", "StationPicker", "Button"} <= component_names

    tool = build_create_a2ui_surface_tool()
    result = tool(
        surface_id=surface_id,
        components=components,
        data_model=update_data_model["updateDataModel"]["value"],
        # clio-workspace is EarthScope's default catalog (v15 S8), so a station
        # surface names its own catalog -- the id instructions.md states.
        catalog_id=str(create_block["createSurface"]["catalogId"]),
    )
    return result, surface_id


def _selected_action(
    context: dict[str, Any], timestamp: str, *, surface_id: str = "earthscope-stations"
) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": "earthscope.stations.selected",
            "surfaceId": surface_id,
            "sourceComponentId": "confirmButton",
            "timestamp": timestamp,
            "context": context,
        },
    }


def _find_values(node: Any, key: str) -> Iterator[Any]:
    """Yield every value bound to ``key`` anywhere in a nested JSON structure."""

    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _find_values(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _find_values(item, key)


def _pending_question(session_id: str, *, surface_id: str) -> UserQuestion:
    now = datetime.now(timezone.utc).isoformat()
    return UserQuestion(
        id="q_earthscope_1",
        session_id=session_id,
        owner_session_id=session_id,
        attended_session_id=session_id,
        prompt="Which stations should I analyse?",
        created_at=now,
        updated_at=now,
        metadata={"resume_on_answer": True, "a2ui_surface_id": surface_id},
    )


# --------------------------------------------------------------------------- #
# Catalog discovery + capability negotiation                                 #
# --------------------------------------------------------------------------- #


def test_pack_catalog_producible_with_file_sidecar_and_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app, sid = _activated_session(tmp_path, monkeypatch)

    response = client.get(f"/v1/sessions/{sid}/a2ui/catalogs")

    assert response.status_code == 200
    rows = {row["catalogId"]: row for row in response.json()["catalogs"]}
    assert PACK_CATALOG_ID in rows
    pack_row = rows[PACK_CATALOG_ID]
    assert pack_row["producible"] is True
    assert pack_row["file"]["catalogId"] == PACK_CATALOG_ID
    assert pack_row["sidecar"]["catalogId"] == PACK_CATALOG_ID
    assert pack_row["instructions"].strip()
    assert {"StationMap", "StationPicker"} <= set(pack_row["componentNames"])


def test_pack_catalog_listed_in_server_wide_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app, _sid = _activated_session(tmp_path, monkeypatch)

    response = client.get("/v1/capabilities", headers=HEADERS)

    assert response.status_code == 200
    ids = response.json()["capabilities"]["a2ui_capabilities"]["v0.9"]["supportedCatalogIds"]
    assert PACK_CATALOG_ID in ids


def test_unnamed_selection_follows_the_agent_order_not_the_client_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EarthScope declares clio-workspace first (v15 S8); the client listing the
    pack catalog first does not change the agent's default."""

    _client, app, sid = _activated_session(tmp_path, monkeypatch)
    _advertise_pack_first(app, sid)

    assert select_catalog(app, sid).catalog_id == WORKSPACE_CATALOG_ID
    named = select_catalog(app, sid, preferred=PACK_CATALOG_ID)
    assert named.catalog_id == PACK_CATALOG_ID
    assert named.reason is None


def test_instructions_state_the_catalog_id_a_station_surface_names() -> None:
    text = _instructions_text()
    assert f"`{PACK_CATALOG_ID}`" in text
    assert "`create_a2ui_surface`'s `catalog_id`" in text


# --------------------------------------------------------------------------- #
# Producer tool: the worked example, naming its catalog                       #
# --------------------------------------------------------------------------- #


def test_create_surface_from_worked_example_naming_its_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path, monkeypatch)
    _advertise_pack_first(app, sid)

    result, surface_id = _create_example_surface(app, sid, monkeypatch)

    assert result.get("ok") is not False, result
    assert result["rendered"] is True
    assert result["created"] is True
    assert result["catalog_id"] == PACK_CATALOG_ID
    surface = app.state.a2ui_store.get(sid, surface_id)
    assert surface is not None
    assert surface.catalog_id == PACK_CATALOG_ID
    assert surface.state == "ready"


# --------------------------------------------------------------------------- #
# Event dispatch: idle delivery, duplicate, invalid context                  #
# --------------------------------------------------------------------------- #


def test_idle_action_delivers_and_starts_a_turn_with_context_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path, monkeypatch)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    _stub_spawn(app, monkeypatch)
    action = _selected_action(
        {"searchId": "earthscope-la-20260917", "stationIds": ["P123", "P456"]},
        "2026-09-17T00:00:00Z",
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "delivered"
    assert body["delivery"] == "start"
    surface = app.state.a2ui_store.get(sid, "earthscope-stations")
    assert surface is not None
    assert len(surface.actions) == 1
    assert surface.actions[0]["state"] == "delivered"
    user_messages = [m for m in app.state.messages[sid] if m.role == "user"]
    assert user_messages[-1].metadata["a2ui_action_context"] == {
        "searchId": "earthscope-la-20260917",
        "stationIds": ["P123", "P456"],
    }


def test_duplicate_submission_is_one_record_and_one_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path, monkeypatch)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    spawned = _stub_spawn(app, monkeypatch)
    action = _selected_action(
        {"searchId": "s1", "stationIds": ["P123", "P456"]}, "2026-09-17T00:00:00Z"
    )

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["action_id"] == second.json()["action_id"]
    assert len(spawned) == 1
    surface = app.state.a2ui_store.get(sid, "earthscope-stations")
    assert surface is not None
    assert len(surface.actions) == 1


def test_empty_station_ids_is_422_event_context_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path, monkeypatch)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    _stub_spawn(app, monkeypatch)
    action = _selected_action({"searchId": "s1", "stationIds": []}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_event_context_invalid"


# --------------------------------------------------------------------------- #
# The catalog as a generated skill                                           #
# --------------------------------------------------------------------------- #


def test_skills_block_lists_the_pack_catalog_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path, monkeypatch)
    root = AgentDef(id="root", title="Root", module={"kind": "react"})

    runtime = skill_runtime_for_agent(app, root, session_id=sid)

    assert CATALOG_SKILL_ID in runtime.resolved


def test_load_skill_station_picker_fragment_is_fixed_to_multiple_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path, monkeypatch)
    root = AgentDef(id="root", title="Root", module={"kind": "react"})
    runtime = skill_runtime_for_agent(app, root, session_id=sid)
    tool = build_load_skill_tool(root, runtime)

    out = tool.func(skill_id=CATALOG_SKILL_ID, file="catalog.json#/components/StationPicker")

    definition, _end = json.JSONDecoder().raw_decode(out)
    component_schemas = list(_find_values(definition, "component"))
    assert any(
        isinstance(schema, dict) and schema.get("const") == "StationPicker"
        for schema in component_schemas
    )
    variant_schemas = list(_find_values(definition, "variant"))
    assert variant_schemas, "StationPicker schema names no 'variant' property"
    assert any(schema.get("const") == "multipleSelection" for schema in variant_schemas)


# --------------------------------------------------------------------------- #
# waiting_user scene                                                         #
# --------------------------------------------------------------------------- #


def test_waiting_user_scene_resumes_the_correlated_question_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path, monkeypatch)
    app.state.agent = object()
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    _stub_spawn(app, monkeypatch)
    question = _pending_question(sid, surface_id="earthscope-stations")
    app.state.user_questions[question.id] = question
    app.state.sessions.update(
        sid, status="waiting_user", metadata_patch={"pending_user_question_id": question.id}
    )
    action = _selected_action(
        {"searchId": "s1", "stationIds": ["P123", "P456"]}, "2026-09-17T00:00:00Z"
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "resolve_question"
    answered = app.state.user_questions[question.id]
    assert answered.status == "answered"
    assert answered.answer_metadata is not None
    assert answered.answer_metadata["surface_id"] == "earthscope-stations"
    assert answered.answer_metadata["a2ui_action_context"] == {
        "searchId": "s1",
        "stationIds": ["P123", "P456"],
    }
