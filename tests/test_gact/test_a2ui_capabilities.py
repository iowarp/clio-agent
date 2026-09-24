"""A2UI 0.9 capability negotiation, per-session client memory, catalog
selection, sub-agent stripping, and the GACT wire doors (S3,
docs/design/a2ui-compat-campaign-2026-09.md, issue #1369).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIAgentCapabilities
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_capabilities import (
    A2UI_CLIENT_CAPABILITIES_METADATA_KEY,
    A2UI_CLIENT_DATA_MODEL_METADATA_KEY,
    A2UICapabilitiesError,
    CatalogSelection,
    agent_capabilities,
    blueprint_a2ui_capability_ids,
    client_capabilities,
    parse_client_capabilities,
    parse_client_data_model,
    remember_client_capabilities,
    select_catalog,
    session_requested_send_data_model,
    strip_renderer_metadata,
)
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.agent_message_transport import message_in_process
from clio_agent.gact.app import build_app

from .a2ui_catalog_binding import bind_builtin_catalogs

pytestmark = pytest.mark.usefixtures("host_agent_executor")

A2UI_HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
BASIC_ID = basic_catalog_id()
WORKSPACE_ID = workspace_catalog_id()


class FakeClioAgent:
    """Minimal stand-in for ClioAgent (no LM needed)."""

    def __init__(self, answer: str = "hello from fake") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        return SimpleNamespace(
            answer=self.answer,
            selected_expert="",
            routing_rationale="",
            route_source="",
            route_reason="",
            error_info=None,
        )


@pytest.fixture()
def fake_agent() -> FakeClioAgent:
    return FakeClioAgent()


@pytest.fixture()
def client(tmp_path: Path, fake_agent: FakeClioAgent) -> Iterator[TestClient]:
    with TestClient(
        build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    ) as test_client:
        yield test_client


def _create_session(client: TestClient, title: str = "t") -> str:
    return client.post("/v1/sessions", json={"title": title}).json()["id"]


def _wait_idle(client: TestClient, sid: str, timeout: float = 10.0) -> None:
    runner = client.app.state.turn_runner
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not runner.busy(sid):
            return
        time.sleep(0.02)
    raise TimeoutError(f"session {sid} still busy after {timeout}s")


# --------------------------------------------------------------------------- #
# parse_client_capabilities / parse_client_data_model                        #
# --------------------------------------------------------------------------- #


def test_parse_client_capabilities_absent_is_none() -> None:
    assert parse_client_capabilities(None) is None
    assert parse_client_capabilities({}) is None
    assert parse_client_capabilities({"other": 1}) is None


def test_parse_client_capabilities_valid() -> None:
    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID, BASIC_ID]}}}
    )
    assert caps is not None
    assert caps.v0_9.supportedCatalogIds == [WORKSPACE_ID, BASIC_ID]


def test_parse_client_capabilities_malformed_raises_typed_reason() -> None:
    with pytest.raises(A2UICapabilitiesError) as excinfo:
        parse_client_capabilities(
            {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": "not-a-list"}}}
        )
    assert excinfo.value.reason == "a2ui_client_capabilities_invalid"


def test_parse_client_capabilities_inline_catalogs_refused() -> None:
    with pytest.raises(A2UICapabilitiesError) as excinfo:
        parse_client_capabilities(
            {
                "a2uiClientCapabilities": {
                    "v0.9": {
                        "supportedCatalogIds": [WORKSPACE_ID],
                        "inlineCatalogs": [{"catalogId": "z"}],
                    }
                }
            }
        )
    assert excinfo.value.reason == "a2ui_inline_catalogs_unsupported"


def test_parse_client_data_model_absent_is_none() -> None:
    assert parse_client_data_model(None) is None
    assert parse_client_data_model({}) is None


def test_parse_client_data_model_valid() -> None:
    model = parse_client_data_model(
        {"a2uiClientDataModel": {"version": "v0.9.1", "surfaces": {"s1": {"x": 1}}}}
    )
    assert model is not None
    assert model.surfaces == {"s1": {"x": 1}}


def test_parse_client_data_model_malformed_raises_typed_reason() -> None:
    with pytest.raises(A2UICapabilitiesError) as excinfo:
        parse_client_data_model({"a2uiClientDataModel": {"version": "v0.9.1"}})
    assert excinfo.value.reason == "a2ui_client_data_model_invalid"


# --------------------------------------------------------------------------- #
# remember/client_capabilities: session-scoped memory, restart survival      #
# --------------------------------------------------------------------------- #


def test_remember_and_read_back_client_capabilities(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    app = build_app(sessions_path=sessions_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="t")

    assert client_capabilities(app, session.id) is None

    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}}
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)

    read_back = client_capabilities(app, session.id)
    assert read_back is not None
    assert read_back.v0_9.supportedCatalogIds == [WORKSPACE_ID]
    stored = app.state.sessions.get(session.id).metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY]
    assert stored == {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}


def test_remembered_capabilities_survive_a_restart(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    app1 = build_app(sessions_path=sessions_path)
    session = app1.state.sessions.create(workspace_id="ws_default", title="t")
    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [BASIC_ID]}}}
    )
    assert caps is not None
    remember_client_capabilities(app1, session.id, caps)

    # A fresh app instance over the SAME sessions file == "process restart".
    app2 = build_app(sessions_path=sessions_path)
    read_back = client_capabilities(app2, session.id)
    assert read_back is not None
    assert read_back.v0_9.supportedCatalogIds == [BASIC_ID]


def test_a_later_advertisement_overwrites_the_earlier_one(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    first = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [BASIC_ID]}}}
    )
    second = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID, BASIC_ID]}}}
    )
    assert first is not None and second is not None
    remember_client_capabilities(app, session.id, first)
    remember_client_capabilities(app, session.id, second)
    read_back = client_capabilities(app, session.id)
    assert read_back is not None
    assert read_back.v0_9.supportedCatalogIds == [WORKSPACE_ID, BASIC_ID]


# --------------------------------------------------------------------------- #
# agent_capabilities                                                          #
# --------------------------------------------------------------------------- #


def test_agent_capabilities_server_wide_matches_official_shape(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    wire = agent_capabilities(app, None)
    validated = A2UIAgentCapabilities.model_validate(wire)
    assert validated.v0_9.acceptsInlineCatalogs is False
    assert set(validated.v0_9.supportedCatalogIds) >= {BASIC_ID, WORKSPACE_ID}


def test_agent_capabilities_session_scoped_is_the_builtin_main_declaration(
    tmp_path: Path,
) -> None:
    """v15 S8: a bare session runs the builtin main, which declares clio-workspace."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    wire = agent_capabilities(app, session.id)
    assert wire["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID]


def test_agent_capabilities_session_scoped_follows_declared_order(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_builtin_catalogs(app, session.id)
    wire = agent_capabilities(app, session.id)
    assert wire["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID, BASIC_ID]


# --------------------------------------------------------------------------- #
# select_catalog                                                              #
# --------------------------------------------------------------------------- #


def test_select_catalog_no_advertisement_is_typed_unknown(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_builtin_catalogs(app, session.id)
    selection = select_catalog(app, session.id)
    assert selection == CatalogSelection(
        catalog_id=None,
        reason="a2ui_client_capabilities_unknown",
        producible_catalog_ids=(WORKSPACE_ID, BASIC_ID),
    )
    assert not selection.ok
    reasons = app.state.a2ui_catalogs.session_reasons(session.id)
    assert any(row["reason"] == "a2ui_client_capabilities_unknown" for row in reasons)


def test_select_catalog_follows_the_agent_declared_order(tmp_path: Path) -> None:
    """v15 S8: the agent's declared order decides, not the client's list order."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_builtin_catalogs(app, session.id)  # declares [clio-workspace, basic]
    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [BASIC_ID, WORKSPACE_ID]}}}
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)
    selection = select_catalog(app, session.id)
    assert selection.ok
    assert selection.catalog_id == WORKSPACE_ID  # first declared, client-supported


def test_select_catalog_no_intersection_is_typed_no_match(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_builtin_catalogs(app, session.id)
    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": ["some/other-catalog"]}}}
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)
    selection = select_catalog(app, session.id)
    assert not selection.ok
    assert selection.reason == "a2ui_catalog_no_client_match"
    reasons = app.state.a2ui_catalogs.session_reasons(session.id)
    assert any(row["reason"] == "a2ui_catalog_no_client_match" for row in reasons)


def test_select_catalog_preferred_wins_only_when_in_both_sets(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_builtin_catalogs(app, session.id)
    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID, BASIC_ID]}}}
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)
    # preferred is producible AND client-supported -> wins over preference order
    assert select_catalog(app, session.id, preferred=BASIC_ID).catalog_id == BASIC_ID
    # preferred not client-supported -> a typed non-selection, NEVER a silent
    # substitution of a different catalog the caller did not ask for.
    selection = select_catalog(app, session.id, preferred="unsupported/catalog")
    assert selection.catalog_id is None
    assert selection.reason == "a2ui_preferred_catalog_not_selectable"
    reasons = app.state.a2ui_catalogs.session_reasons(session.id)
    assert any(
        row["reason"] == "a2ui_preferred_catalog_not_selectable"
        and row["preferred_catalog_id"] == "unsupported/catalog"
        and row["intersection"] == [WORKSPACE_ID, BASIC_ID]
        for row in reasons
    )


# --------------------------------------------------------------------------- #
# strip_renderer_metadata                                                     #
# --------------------------------------------------------------------------- #


def test_strip_renderer_metadata_removes_only_renderer_keys() -> None:
    stripped = strip_renderer_metadata(
        {
            "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}},
            "a2ui_client_capabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}},
            "a2uiClientDataModel": {"version": "v0.9", "surfaces": {}},
            "a2ui_client_data_model": {"version": "v0.9", "surfaces": {}},
            "keep_me": "yes",
        }
    )
    assert stripped == {"keep_me": "yes"}


def test_strip_renderer_metadata_is_a_noop_on_falsy_input() -> None:
    assert strip_renderer_metadata(None) == {}
    assert strip_renderer_metadata({}) == {}


# --------------------------------------------------------------------------- #
# session_requested_send_data_model                                          #
# --------------------------------------------------------------------------- #


def test_session_requested_send_data_model_false_with_no_surfaces(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    assert session_requested_send_data_model(app, session.id) is False


# --------------------------------------------------------------------------- #
# blueprint_a2ui_capability_ids                                              #
# --------------------------------------------------------------------------- #


def test_blueprint_a2ui_capability_ids_is_empty_without_a_declaring_blueprint(
    tmp_path: Path,
) -> None:
    """v15 S8: no blueprint (or an unresolved one) declares nothing -- no builtins."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    assert blueprint_a2ui_capability_ids(app, "") == []
    assert blueprint_a2ui_capability_ids(app, "no-such-blueprint") == []


# --------------------------------------------------------------------------- #
# agent_message_transport.message_in_process: a neutral transport (S3        #
# adversarial review, item 11) -- it no longer strips anything itself. The   #
# client-facing steer HTTP door applies the real guard on the CHILD session  #
# (tested below, under "POST /v1/agent-tasks/{id}/steer"); the model-facing  #
# message_agent tool never supplies metadata at all (nothing to strip).      #
# --------------------------------------------------------------------------- #


def test_message_in_process_passes_metadata_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_enqueue(app: Any, task: Any, text: str, metadata: Any) -> None:
        captured["metadata"] = metadata

    monkeypatch.setattr("clio_agent.gact.live_handle.enqueue_steer_or_raise", _fake_enqueue)
    fake_task = SimpleNamespace(task_id="t1")
    fake_app = SimpleNamespace(
        state=SimpleNamespace(agent_task_registry=SimpleNamespace(get=lambda task_id: fake_task))
    )
    invoker = SimpleNamespace(app=fake_app)
    handle = SimpleNamespace(task_id="t1")

    metadata = {
        "a2ui_client_capabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}},
        "a2ui_client_data_model": {"version": "v0.9", "surfaces": {}},
        "keep_me": "yes",
    }
    message_in_process(invoker, handle, "steer text", metadata)

    assert captured["metadata"] == metadata
    assert captured["metadata"] is not metadata  # a defensive copy, not the same dict


def test_message_in_process_handles_no_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_enqueue(app: Any, task: Any, text: str, metadata: Any) -> None:
        captured["metadata"] = metadata

    monkeypatch.setattr("clio_agent.gact.live_handle.enqueue_steer_or_raise", _fake_enqueue)
    fake_task = SimpleNamespace(task_id="t1")
    fake_app = SimpleNamespace(
        state=SimpleNamespace(agent_task_registry=SimpleNamespace(get=lambda task_id: fake_task))
    )
    invoker = SimpleNamespace(app=fake_app)
    handle = SimpleNamespace(task_id="t1")

    message_in_process(invoker, handle, "steer text", None)

    assert captured["metadata"] == {}


# --------------------------------------------------------------------------- #
# sub-agent stripping: turn_spawn._launch (a REAL spawned child turn)        #
# --------------------------------------------------------------------------- #


class _StubChildAgent:
    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        return SimpleNamespace(
            answer=f"child did: {question[:20]}",
            selected_expert="",
            routing_rationale="",
        )


def test_spawn_child_turn_never_forwards_renderer_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child's FIRST staged user message never carries the two renderer keys,
    even though the parent SESSION carries a remembered client-capabilities
    advertisement under an unrelated key -- proves the invariant end to end
    through the real ``spawn_child_turn_threadsafe`` -> ``_launch`` path."""

    from clio_agent.gact.turn_spawn import TaskSpec, spawn_child_turn_threadsafe

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="", **_bindings: {"main"},
    )
    # NOTE: uses the module-level ``build_app`` (imported at top of this file, and
    # wrapped by the autouse ``_default_test_arc`` fixture) so this app gets a real
    # per-test ARC -- a fresh local import here would bypass that wrapping.
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubChildAgent())
    with TestClient(app) as test_client:
        parent = test_client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        # The parent session carries a remembered client-capabilities advertisement
        # -- present on the PARENT, structurally irrelevant to the child (proves the
        # child's staged message metadata isn't a copy of ANY parent state).
        caps = parse_client_capabilities(
            {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}}
        )
        assert caps is not None
        remember_client_capabilities(app, parent, caps)

        task = spawn_child_turn_threadsafe(
            app,
            TaskSpec(
                child_expert_id="main",
                task_text="analyze the dataset",
                parent_session_id=parent,
                requesting_expert_id="main",
            ),
        )

        # Wait on the task's own completion primitive (turn_spawn.py's
        # AgentTaskRegistry.event -- a threading.Event set on terminal
        # transition, the S6 wait primitive `wait_agent_tasks` itself
        # blocks on) rather than a bare sleep-poll: efficient (no polling
        # granularity to tune) and correct across the thread boundary
        # (TestClient runs the app's event loop on its own thread). The
        # ceiling stays generous -- a cold first turn imports litellm,
        # measured ~9.3s under load even with LITELLM_LOCAL_MODEL_COST_MAP
        # pinned (see conftest.py) -- rather than fixed at exactly the
        # measured worst case.
        completed = app.state.agent_task_registry.event(task.task_id).wait(timeout=30.0)
        settled = app.state.agent_task_registry.get(task.task_id)
        assert completed and settled is not None and settled.status == "completed", (
            settled.status if settled else "no task"
        )

        child_messages = app.state.messages.get(settled.child_session_id, [])
        user_messages = [m for m in child_messages if m.role == "user"]
        assert user_messages, "child never received its staged user message"
        staged_metadata = user_messages[0].metadata
        assert "a2uiClientCapabilities" not in staged_metadata
        assert "a2uiClientDataModel" not in staged_metadata
        assert "a2ui_client_data_model" not in staged_metadata
        assert staged_metadata.get("agent_task_id") == task.task_id


# --------------------------------------------------------------------------- #
# GET /v1/capabilities carries the official a2ui_capabilities object          #
# --------------------------------------------------------------------------- #


def test_v1_capabilities_carries_official_a2ui_capabilities_object(
    client: TestClient,
) -> None:
    resp = client.get("/v1/capabilities", headers=A2UI_HEADERS)
    assert resp.status_code == 200
    wire = resp.json()["capabilities"]["a2ui_capabilities"]
    validated = A2UIAgentCapabilities.model_validate(wire)
    assert validated.v0_9.acceptsInlineCatalogs is False
    assert set(validated.v0_9.supportedCatalogIds) >= {BASIC_ID, WORKSPACE_ID}


# --------------------------------------------------------------------------- #
# GET /v1/sessions/{sid}/a2ui/capabilities                                   #
# --------------------------------------------------------------------------- #


def test_session_capabilities_route_unknown_session_404(client: TestClient) -> None:
    resp = client.get("/v1/sessions/does-not-exist/a2ui/capabilities")
    assert resp.status_code == 404


def test_session_capabilities_route_shape(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"/v1/sessions/{sid}/a2ui/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"]["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID]
    assert body["client"] is None
    assert body["selection"]["reason"] == "a2ui_client_capabilities_unknown"

    post = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {
                "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}
            },
        },
    )
    assert post.status_code == 200, post.text
    _wait_idle(client, sid)

    resp2 = client.get(f"/v1/sessions/{sid}/a2ui/capabilities")
    body2 = resp2.json()
    assert body2["client"]["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID]
    assert body2["selection"]["catalog_id"] == WORKSPACE_ID
    assert body2["selection"]["reason"] is None


# --------------------------------------------------------------------------- #
# POST /v1/sessions/{sid}/messages door: parse + remember + guard            #
# --------------------------------------------------------------------------- #


def test_post_message_malformed_capabilities_422(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": "nope"}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_client_capabilities_invalid"
    reasons = client.app.state.a2ui_catalogs.session_reasons(sid)
    assert any(row["reason"] == "a2ui_client_capabilities_invalid" for row in reasons)


def test_post_message_inline_catalogs_422(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {
                "a2uiClientCapabilities": {
                    "v0.9": {
                        "supportedCatalogIds": [WORKSPACE_ID],
                        "inlineCatalogs": [{"catalogId": "z"}],
                    }
                }
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_inline_catalogs_unsupported"


def test_post_message_data_model_without_a_requesting_surface_422(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {"a2uiClientDataModel": {"version": "v0.9.1", "surfaces": {"s1": {}}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_data_model_not_requested"


def test_post_message_valid_capabilities_are_remembered(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [BASIC_ID]}}},
        },
    )
    assert resp.status_code == 200, resp.text
    stored = client.app.state.sessions.get(sid).metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY]
    assert stored == {"v0.9": {"supportedCatalogIds": [BASIC_ID]}}


def test_post_message_data_model_is_carried_through_renamed(client: TestClient) -> None:
    sid = _create_session(client)
    create = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=A2UI_HEADERS,
        json={
            "messages": [
                {
                    "version": "v0.9.1",
                    "createSurface": {
                        "surfaceId": "surface_1",
                        "catalogId": WORKSPACE_ID,
                        "sendDataModel": True,
                    },
                }
            ]
        },
    )
    assert create.status_code == 200, create.text

    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {
                "a2uiClientDataModel": {
                    "version": "v0.9.1",
                    "surfaces": {"surface_1": {"x": 1}},
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["message_id"]
    stored_message = next(m for m in client.app.state.messages.get(sid, []) if m.id == user_id)
    assert A2UI_CLIENT_DATA_MODEL_METADATA_KEY in stored_message.metadata
    assert "a2uiClientDataModel" not in stored_message.metadata
    assert stored_message.metadata[A2UI_CLIENT_DATA_MODEL_METADATA_KEY] == {
        "version": "v0.9.1",
        "surfaces": {"surface_1": {"x": 1}},
    }


# --------------------------------------------------------------------------- #
# A2UI action route: optional top-level metadata                             #
# --------------------------------------------------------------------------- #


def test_action_route_rejects_unknown_top_level_fields(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=A2UI_HEADERS,
        json={"message": {}, "unexpected": True},
    )
    assert resp.status_code == 422


def test_action_route_accepts_and_remembers_metadata(client: TestClient) -> None:
    sid = _create_session(client)
    create = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=A2UI_HEADERS,
        json={
            "messages": [
                {
                    "version": "v0.9.1",
                    "createSurface": {"surfaceId": "surface_1", "catalogId": WORKSPACE_ID},
                }
            ]
        },
    )
    assert create.status_code == 200, create.text

    resp = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=A2UI_HEADERS,
        json={
            "message": {
                "version": "v0.9.1",
                "action": {
                    "surfaceId": "surface_1",
                    "sourceComponentId": "root",
                    "name": "approval.respond",
                    "timestamp": "2026-09-16T00:00:00Z",
                    "context": {"permission_id": "does-not-exist", "action": "allow"},
                },
            },
            "metadata": {
                "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}
            },
        },
    )
    # The action itself 404s (unknown permission_id) -- the point of this test is
    # that "metadata" is an accepted top-level key and is applied BEFORE dispatch.
    assert resp.status_code == 404, resp.text
    stored = client.app.state.sessions.get(sid).metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY]
    assert stored == {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}


def test_action_route_malformed_capabilities_422(client: TestClient) -> None:
    sid = _create_session(client)
    create = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=A2UI_HEADERS,
        json={
            "messages": [
                {
                    "version": "v0.9.1",
                    "createSurface": {"surfaceId": "surface_1", "catalogId": WORKSPACE_ID},
                }
            ]
        },
    )
    assert create.status_code == 200, create.text

    resp = client.post(
        f"/v1/sessions/{sid}/a2ui/actions",
        headers=A2UI_HEADERS,
        json={
            "message": {
                "version": "v0.9.1",
                "action": {
                    "surfaceId": "surface_1",
                    "sourceComponentId": "root",
                    "name": "form.submit",
                    "timestamp": "2026-09-16T00:00:00Z",
                    "context": {},
                },
            },
            "metadata": {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": "nope"}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_client_capabilities_invalid"


# --------------------------------------------------------------------------- #
# GET /v1/agents rows carry per-blueprint a2ui_capabilities                  #
# --------------------------------------------------------------------------- #


def test_agent_rows_carry_a2ui_capabilities(client: TestClient) -> None:
    resp = client.get("/v1/agents")
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert rows, "no agent rows returned"
    for row in rows:
        ids = row["metadata"]["a2ui_capabilities"]
        assert isinstance(ids, list)
        if row["metadata"].get("definition_kind") == "builtin_main":
            assert ids == [WORKSPACE_ID]  # v15 S8: the builtin main's own declaration
        elif not row["metadata"].get("agent_blueprint_id"):
            assert ids == []  # no declaring unit, no catalogs


def test_agent_detail_route_carries_a2ui_capabilities(client: TestClient) -> None:
    listed = client.get("/v1/agents").json()["agents"]
    agent_id = listed[0]["id"]
    resp = client.get(f"/v1/agents/{agent_id}")
    assert resp.status_code == 200
    metadata = resp.json()["metadata"]
    assert isinstance(metadata["a2ui_capabilities"], list)
    if metadata.get("definition_kind") == "builtin_main":
        assert metadata["a2ui_capabilities"] == [WORKSPACE_ID]
    elif not metadata.get("agent_blueprint_id"):
        assert metadata["a2ui_capabilities"] == []


# --------------------------------------------------------------------------- #
# POST /v1/agent-tasks/{id}/steer: the SAME door guard, applied to the       #
# CHILD session (S3 adversarial review, item 11)                            #
# --------------------------------------------------------------------------- #


class _SlowChildAgent:
    """Stays "running" long enough for one or two steers to land before settling."""

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        time.sleep(4.0)
        return SimpleNamespace(answer=f"child did: {question[:20]}", selected_expert="")


@pytest.fixture()
def running_child_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
    """Yield ``(test_client, task_id)`` for a child STILL RUNNING (the stub
    agent sleeps 4s before answering) -- everything (spawn AND steer) stays
    within the SAME TestClient context, since exiting/re-entering a fresh one
    triggers app shutdown/startup that consumes the sleep window and lets the
    child settle before the test ever gets to steer it."""

    from clio_agent.gact.turn_spawn import TaskSpec, spawn_child_turn_threadsafe

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="", **_bindings: {"main"},
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowChildAgent())
    with TestClient(app) as test_client:
        parent = test_client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = spawn_child_turn_threadsafe(
            app,
            TaskSpec(
                child_expert_id="main",
                task_text="analyze the dataset",
                parent_session_id=parent,
                requesting_expert_id="main",
            ),
        )
        yield test_client, task.task_id


def test_steer_door_malformed_capabilities_422_and_not_remembered(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    resp = test_client.post(
        f"/v1/agent-tasks/{task_id}/steer",
        json={
            "text": "hello child",
            "metadata": {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": "nope"}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_client_capabilities_invalid"

    task = app.state.agent_task_registry.get(task_id)
    child = app.state.sessions.get(task.child_session_id)
    assert A2UI_CLIENT_CAPABILITIES_METADATA_KEY not in child.metadata
    reasons = app.state.a2ui_catalogs.session_reasons(task.child_session_id)
    assert any(row["reason"] == "a2ui_client_capabilities_invalid" for row in reasons)


def test_steer_door_valid_capabilities_remembered_on_child_session(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    resp = test_client.post(
        f"/v1/agent-tasks/{task_id}/steer",
        json={
            "text": "hello child",
            "metadata": {
                "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}
            },
        },
    )
    assert resp.status_code == 202, resp.text

    task = app.state.agent_task_registry.get(task_id)
    child = app.state.sessions.get(task.child_session_id)
    assert child.metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY] == {
        "v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}
    }


def test_steer_door_data_model_checked_against_the_childs_own_surfaces(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    task = app.state.agent_task_registry.get(task_id)
    child_sid = task.child_session_id

    # No surface on the CHILD requested sendDataModel yet -> refused.
    resp = test_client.post(
        f"/v1/agent-tasks/{task_id}/steer",
        json={
            "text": "hello child",
            "metadata": {"a2uiClientDataModel": {"version": "v0.9.1", "surfaces": {"s1": {}}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_data_model_not_requested"

    # Once the CHILD's own surface requests it, the same data model is accepted.
    create = test_client.post(
        f"/v1/sessions/{child_sid}/a2ui/messages",
        headers=A2UI_HEADERS,
        json={
            "messages": [
                {
                    "version": "v0.9.1",
                    "createSurface": {
                        "surfaceId": "s1",
                        "catalogId": WORKSPACE_ID,
                        "sendDataModel": True,
                    },
                }
            ]
        },
    )
    assert create.status_code == 200, create.text

    resp2 = test_client.post(
        f"/v1/agent-tasks/{task_id}/steer",
        json={
            "text": "hello again",
            "metadata": {
                "a2uiClientDataModel": {"version": "v0.9.1", "surfaces": {"s1": {"x": 1}}}
            },
        },
    )
    assert resp2.status_code == 202, resp2.text


# --------------------------------------------------------------------------- #
# Pack-blueprint end to end: a PATH-activated session with a declared        #
# catalog (S2 fixture pack) is visible everywhere a2ui_capabilities reaches  #
# (S3 adversarial review, item 7)                                            #
# --------------------------------------------------------------------------- #

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "minimal"
_PACK_CATALOG_ID = "https://example.test/a2ui/catalogs/minimal"


def _activate_minimal_pack(app: Any, sid: str) -> None:
    app.state.sessions.update(
        sid,
        metadata_patch={
            "active_agent_blueprint_id": "a2ui-minimal-pack",
            "active_agent_blueprint_path": str(_FIXTURE_PACK),
        },
    )


def test_pack_blueprint_session_capabilities_route_lists_the_pack_catalog(
    client: TestClient,
) -> None:
    sid = _create_session(client)
    _activate_minimal_pack(client.app, sid)

    resp = client.get(f"/v1/sessions/{sid}/a2ui/capabilities")
    assert resp.status_code == 200
    assert _PACK_CATALOG_ID in resp.json()["agent"]["v0.9"]["supportedCatalogIds"]


def test_pack_blueprint_select_catalog_picks_it_when_client_lists_it_first(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    _activate_minimal_pack(app, session.id)

    caps = parse_client_capabilities(
        {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [_PACK_CATALOG_ID, BASIC_ID]}}}
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)

    selection = select_catalog(app, session.id)
    assert selection.catalog_id == _PACK_CATALOG_ID


def test_pack_blueprint_agent_row_for_that_session_carries_it(client: TestClient) -> None:
    sid = _create_session(client)
    _activate_minimal_pack(client.app, sid)

    resp = client.get(f"/v1/agents?session_id={sid}")
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert rows, "the path-activated blueprint produced no agent rows"
    assert any(
        row["metadata"].get("agent_blueprint_id") == "a2ui-minimal-pack"
        and _PACK_CATALOG_ID in row["metadata"]["a2ui_capabilities"]
        for row in rows
    ), rows


# --------------------------------------------------------------------------- #
# GET /v1/agent-blueprints/{id} detail rows carry a2ui_capabilities          #
# (S3 adversarial review, item 12 / spec item 4)                            #
# --------------------------------------------------------------------------- #


def test_agent_blueprint_detail_route_carries_a2ui_capabilities(
    client: TestClient, tmp_path: Path
) -> None:
    import shutil

    marketplace = tmp_path / "marketplace"
    shutil.copytree(_FIXTURE_PACK, marketplace / "a2ui-minimal-pack")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    wid = client.post(
        "/v1/workspaces",
        json={
            "name": "Workspace",
            "root_path": str(workspace),
            "storage_root": str(workspace / ".clio"),
        },
    ).json()["id"]
    installed = client.post(
        "/v1/agent-blueprints/install",
        json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
    )
    assert installed.status_code == 201, installed.text

    detail = client.get("/v1/agent-blueprints/a2ui-minimal-pack", params={"workspace_id": wid})
    assert detail.status_code == 200, detail.text
    # v15 S8: exactly what the blueprint declares (the fixture lists only its
    # own catalog), never the builtins implicitly.
    assert detail.json()["a2ui_capabilities"] == [_PACK_CATALOG_ID]
