"""EarthScope plugin A2UI catalog composability proof (S7).

docs/design/a2ui-compat-campaign-2026-09.md S7 / iowarp/clio-agent-marketplace
issue #69 deliverable 4 (the clio-agent half): the campaign's composability
proof runs against an UNMODIFIED clio-agent server. A marketplace plugin,
``external/clio-agent-marketplace/plugins/earthscope-skills/``, ships its OWN
A2UI catalog (``ai.iowarp.clio/a2ui/earthscope-stations/{catalog.json,
catalog.clio.json, instructions.md}``) exactly the way it ships skills and
prompt fragments. This installs that REAL plugin (and the plugins it requires,
from the pinned submodule -- never a fixture stand-in) into the test's isolated
plugin store, adds it to a session's agent, and proves every S2-S5 seam accepts
it: catalog discovery, server-wide and per-client capability negotiation, the
producer tool with the worked example parsed straight out of
``instructions.md`` (so this test cannot drift from the docs), the event
dispatcher's idle/duplicate/invalid-context paths, the generated catalog skill,
and a ``waiting_user`` resume.

Every assertion reads the STORED/served value (the app's own
``a2ui_store``/``messages``/``user_questions`` state, or the HTTP response
body), never a client-side echo.

The waiting_user/idempotency harness mirrors ``tests/test_gact/test_a2ui_actions.py``'s
``_pending_question``/``_stub_spawn``.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import QuestionDefinition, QuestionReply, TaskSnapshot
from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities, select_catalog
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.agents.resolution import plugin_service, session_agent_def
from clio_agent.gact.agents.skill_runtime import build_load_skill_tool, skill_runtime_for_agent
from clio_agent.gact.app import build_app
from tests._marketplace import MARKETPLACE_ROOT

from .a2ui_catalog_binding import bind_session_plugins

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}

pytestmark = [pytest.mark.marketplace, pytest.mark.usefixtures("native_builtin_lifespan")]

MARKETPLACE_PLUGINS = MARKETPLACE_ROOT / "plugins"
PLUGIN_ID = "earthscope-skills"
PLUGIN_ROOT = MARKETPLACE_PLUGINS / PLUGIN_ID
PACK_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/earthscope-stations/v1"
BASIC_CATALOG_ID = basic_catalog_id()
WORKSPACE_CATALOG_ID = workspace_catalog_id()
CATALOG_SKILL_ID = "a2ui-catalog-earthscope-stations"

_CATALOG_DIR = PLUGIN_ROOT / "ai.iowarp.clio" / "a2ui" / "earthscope-stations"
_INSTRUCTIONS_PATH = _CATALOG_DIR / "instructions.md"


# --------------------------------------------------------------------------- #
# Install + bind + capability-advertisement harness                          #
# --------------------------------------------------------------------------- #


def _plugin_requirements() -> list[str]:
    """The REAL plugin's declared requirements, read from its own manifest."""

    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    return list(manifest["extensions"]["ai.iowarp.clio"].get("requires") or [])


def _activated_session(tmp_path: Path) -> tuple[TestClient, Any, str]:
    """Install the real plugin (and what it requires) and add it to a fresh session.

    Each plugin installs from its own submodule folder into the app's plugin
    store, which ``tests/conftest.py`` isolates under ``tmp_path``; the MCP-only
    requirements carry executable parts, so they pass the explicit trust step.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")
    store = plugin_service(app).store
    assert store.root.resolve().is_relative_to(tmp_path.resolve()), store.root
    requirements = _plugin_requirements()
    for plugin_id in requirements:
        store.install(str(MARKETPLACE_PLUGINS / plugin_id), name=plugin_id)
        store.trust(plugin_id)
    session = app.state.sessions.create(workspace_id="ws_default", title="EarthScope plugin")
    resolution = bind_session_plugins(app, session.id, PLUGIN_ID, root=MARKETPLACE_PLUGINS)
    assert {PLUGIN_ID, *requirements} <= set(resolution.plugin_ids)
    return TestClient(app), app, session.id


def _advertise_pack_first(app: Any, session_id: str) -> None:
    """Advertise client capabilities listing the plugin catalog first.

    The client also renders clio-workspace and Basic, as a real client does.
    """

    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": [PACK_CATALOG_ID, WORKSPACE_CATALOG_ID, BASIC_CATALOG_ID]}}
    )
    remember_client_capabilities(app, session_id, caps)


def _hold_segments(app: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Hold model execution after real Core admission, until lifespan shutdown."""
    from tests.test_gact.test_post_messages import FakeClioAgent

    spawned: list[Any] = []
    started = threading.Event()
    # Admission requires an available model owner; only its execution is stubbed.
    monkeypatch.setattr(app.state, "agent", FakeClioAgent())
    monkeypatch.setattr(app.state, "test_spawn_started", started, raising=False)

    async def hold(segment: Any) -> None:
        spawned.append(segment)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.conversations, "_run", hold)
    return spawned


# --------------------------------------------------------------------------- #
# The worked example, parsed out of instructions.md (never hand-copied)      #
# --------------------------------------------------------------------------- #


def _instructions_text() -> str:
    return _INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def _parse_worked_example_block(text: str, label: str) -> dict[str, Any]:
    """Parse one named ```json code block out of the worked example.

    Regex-extracted from the plugin's own docs -- never hand-copied -- so this
    test cannot silently drift from ``instructions.md``.
    """

    pattern = rf"`{re.escape(label)}`:\s*\n\n```json\n(.*?)\n```"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"instructions.md has no {label!r} worked-example block"
    return json.loads(match.group(1))


def _create_example_surface(
    app: Any, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], str]:
    """Create the plugin's worked-example surface through the real producer tool.

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


def _pending_question(app: Any, session_id: str, *, surface_id: str) -> TaskSnapshot:
    """Post the exact visual Question through its production Core owner."""
    assignment = app.state.task_history.latest_assignment(session_id)
    assert assignment is not None
    return app.state.questions.create(
        assignment.owner.model_copy(update={"parent_handle": assignment.handle}),
        QuestionDefinition(
            prompt="Which stations should I analyse?",
            metadata={
                "a2ui_surface_id": surface_id,
                "a2ui_answer_action": "earthscope.stations.selected",
            },
        ),
        invocation_id="earthscope-surface-question",
        handle="q_earthscope_1",
    )


def test_plugin_catalog_producible_with_file_sidecar_and_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app, sid = _activated_session(tmp_path)

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


def test_plugin_catalog_listed_in_server_wide_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app, _sid = _activated_session(tmp_path)

    response = client.get("/v1/capabilities", headers=HEADERS)

    assert response.status_code == 200
    ids = response.json()["capabilities"]["a2ui_capabilities"]["v0.9"]["supportedCatalogIds"]
    assert PACK_CATALOG_ID in ids


def test_unnamed_selection_follows_the_agent_order_not_the_client_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """core's clio-workspace resolves before the EarthScope plugin's own catalog;
    the client listing the plugin catalog first does not change the agent's default."""

    _client, app, sid = _activated_session(tmp_path)
    _advertise_pack_first(app, sid)

    assert select_catalog(app, sid).catalog_id == WORKSPACE_CATALOG_ID
    named = select_catalog(app, sid, preferred=PACK_CATALOG_ID)
    assert named.catalog_id == PACK_CATALOG_ID
    assert named.reason is None


def test_generated_station_skill_states_the_catalog_json_catalog_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The station catalogId reaches the model through the GENERATED catalog
    skill (never hand-typed prose): it equals catalog.json's own id and says the
    station catalog is not EarthScope's default (clio-workspace is)."""

    from clio_agent.gact.skills import SkillCatalog

    _client, app, sid = _activated_session(tmp_path)
    catalog_id = json.loads((_CATALOG_DIR / "catalog.json").read_text(encoding="utf-8"))[
        "catalogId"
    ]

    refs = {ref.id: ref for ref in SkillCatalog(app=app, session_id=sid)._catalog_refs()}
    body = refs[CATALOG_SKILL_ID].body_provider()

    assert catalog_id == PACK_CATALOG_ID
    assert f"Catalog id: `{catalog_id}`" in body
    assert "This is not this agent's default catalog" in body
    assert (
        "This is this agent's default catalog"
        in refs["a2ui-catalog-clio-workspace"].body_provider()
    )


# --------------------------------------------------------------------------- #
# Producer tool: the worked example, naming its catalog                       #
# --------------------------------------------------------------------------- #


def test_create_surface_from_worked_example_naming_its_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path)
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
    client, app, sid = _activated_session(tmp_path)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    spawned = _hold_segments(app, monkeypatch)
    original_assignment = app.state.task_history.latest_assignment(sid)
    assert original_assignment is not None
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
    # Admission belongs to TurnRunner. The producer always reports queued
    # steering; the actual new segment is checked through its Core admission.
    assert body["delivery"] == "steer"
    assert app.state.test_spawn_started.wait(5), "Queued action was not admitted"
    surface = app.state.a2ui_store.get(sid, "earthscope-stations")
    assert surface is not None
    assert len(surface.actions) == 1
    assert surface.actions[0]["state"] == "delivered"
    history, owner = app.state.task_history, app.state.conversations
    mail = history.pending_mail(sid)
    assert len(mail) == 1 and mail[0].kind == "human"
    assert mail[0].sender_human == app.state.human_principal
    queued = history.read_content(mail[0].content)
    expected_context = {
        "searchId": "earthscope-la-20260917",
        "stationIds": ["P123", "P456"],
    }
    assert queued["message"]["metadata"]["a2ui_action_context"] == expected_context
    assert queued["text"] == surface.actions[0]["narration"]
    assert len(spawned) == 1
    assert mail[0].admission is not None
    assignment = history.latest_assignment(sid)
    assert assignment is not None and mail[0].admission.assignment_id == assignment.handle
    assert assignment.handle == original_assignment.handle
    assert mail[0].admission.turn_id == spawned[0].turn_id
    assert mail[0].admission.content == mail[0].content
    assert mail[0].admission.input_revision == mail[0].revision
    staged = owner.mail.stage(owner.caller(sid))
    assert len(staged.items) == 1 and len(staged.injections) == 1
    source, text = staged.injections[0]
    assert source == "steer"
    assert text.endswith(queued["text"])
    assert "Human " + app.state.human_principal in text
    assert text.notification_ids == (mail[0].id,)
    assert history.pending_mail(sid) == mail  # Staging is not recipient input commit.


def test_duplicate_submission_is_one_record_and_one_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    spawned = _hold_segments(app, monkeypatch)
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
    assert app.state.test_spawn_started.wait(5), "Queued action was not admitted"
    assert first.json()["action_id"] == second.json()["action_id"]
    assert len(spawned) == 1
    surface = app.state.a2ui_store.get(sid, "earthscope-stations")
    assert surface is not None
    assert len(surface.actions) == 1


def test_empty_station_ids_is_422_event_context_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path)
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    _hold_segments(app, monkeypatch)
    action = _selected_action({"searchId": "s1", "stationIds": []}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_event_context_invalid"


# --------------------------------------------------------------------------- #
# The catalog as a generated skill                                           #
# --------------------------------------------------------------------------- #


def test_skills_block_lists_the_plugin_catalog_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path)
    agent = session_agent_def(app, sid)

    runtime = skill_runtime_for_agent(app, agent, session_id=sid)

    assert CATALOG_SKILL_ID in runtime.resolved
    assert "a2ui-catalog-clio-workspace" in runtime.resolved


def test_load_skill_station_picker_fragment_is_fixed_to_multiple_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, sid = _activated_session(tmp_path)
    agent = session_agent_def(app, sid)
    runtime = skill_runtime_for_agent(app, agent, session_id=sid)
    tool = build_load_skill_tool(agent, runtime)

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


def test_correlated_async_question_answer_preserves_selected_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, sid = _activated_session(tmp_path)
    app.state.agent = object()
    _advertise_pack_first(app, sid)
    _create_example_surface(app, sid, monkeypatch)
    _hold_segments(app, monkeypatch)
    question = _pending_question(app, sid, surface_id="earthscope-stations")
    assert question.raw_status == "pending"
    action = _selected_action(
        {"searchId": "s1", "stationIds": ["P123", "P456"]}, "2026-09-17T00:00:00Z"
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "resolve_question"
    history = app.state.task_history
    answered = history.task_snapshot(question.handle)
    assert answered is not None and answered.raw_status == "answered"
    assert answered.status == "completed" and answered.result is not None
    reply = QuestionReply.model_validate(history.read_content(answered.result))
    assert reply.source_action is not None
    assert reply.source_action.surface_id == "earthscope-stations"
    assert reply.source_action.session_id == sid
    assert reply.source_action.action_id == response.json()["action_id"]
    assert reply.metadata == {
        "searchId": "s1",
        "stationIds": ["P123", "P456"],
    }
    assert reply.responder_human == app.state.human_principal
    assert any(
        item.kind == "question_answer" and item.handle == question.handle
        for item in history.pending_mail(sid)
    )
