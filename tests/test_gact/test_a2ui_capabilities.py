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
    client_capabilities,
    parse_client_capabilities,
    parse_client_data_model,
    remember_client_capabilities,
    resolution_catalog_ids,
    select_catalog,
    session_requested_send_data_model,
    strip_renderer_metadata,
)
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.agents.resolution import plugin_service
from clio_agent.gact.app import build_app
from clio_agent.plugins.resolve import Delta
from tests.turn_signals import TURN_SIGNAL_BACKSTOP_S

from .a2ui_catalog_binding import (
    A2UI_PLUGINS,
    MINIMAL_CATALOG_ID,
    MINIMAL_PLUGIN,
    bind_builtin_catalogs,
    bind_session_plugins,
    install_plugins,
)

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


def _wait_idle(client: TestClient, sid: str, timeout: float = TURN_SIGNAL_BACKSTOP_S) -> None:
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


def test_agent_capabilities_session_scoped_is_the_standard_agents_declaration(
    tmp_path: Path,
) -> None:
    """A bare session runs ``clio/standard``; only its ``core`` plugin declares a
    catalog (``clio-workspace``)."""

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
# resolution_catalog_ids: an agent's catalogs, straight from its resolution  #
# --------------------------------------------------------------------------- #


def test_resolution_catalog_ids_are_the_resolved_plugins_declarations(tmp_path: Path) -> None:
    """No agent is ever catalog-less (``core`` declares clio-workspace); a plugin
    added to the agent appends its own catalogs after core's."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    service = plugin_service(app)
    standard = service.resolve("clio/standard", pin=False)
    assert resolution_catalog_ids(standard) == [WORKSPACE_ID]

    install_plugins(app, MINIMAL_PLUGIN, "a2ui-basic")
    extended = service.resolve(
        "clio/standard", delta=Delta(add=("a2ui-basic", MINIMAL_PLUGIN)), pin=False
    )
    assert resolution_catalog_ids(extended) == [WORKSPACE_ID, BASIC_ID, MINIMAL_CATALOG_ID]


# --------------------------------------------------------------------------- #
# agent_message_transport.message_in_process: a neutral transport (S3        #
# adversarial review, item 11) -- it no longer strips anything itself. The   #
# client-facing steer HTTP door applies the real guard on the CHILD session  #
# (tested below, under "POST /v1/agent-tasks/{id}/steer"); the model-facing  #
# message_agent tool never supplies metadata at all (nothing to strip).      #
# --------------------------------------------------------------------------- #


def test_session_input_records_a_defensive_metadata_copy(tmp_path: Path) -> None:
    """Renderer metadata reaches Core unchanged and cannot be mutated after acceptance."""
    from clio_agent.gact.session_input import accept_session_input

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="steering")
        metadata = {
            "a2ui_client_capabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}},
            "keep_me": "yes",
        }
        identity = accept_session_input(app, session.id, "steer text", metadata=metadata)
        metadata["keep_me"] = "changed later"
        item = app.state.task_history.mail_item(session.id, identity)
        stored = app.state.task_history.read_content(item.content)
        assert stored["metadata"]["keep_me"] == "yes"
        assert (
            stored["metadata"]["a2ui_client_capabilities"] == metadata["a2ui_client_capabilities"]
        )


def test_session_input_without_metadata_is_an_explicit_empty_mapping(tmp_path: Path) -> None:
    from clio_agent.gact.session_input import accept_session_input

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="steering")
        identity = accept_session_input(app, session.id, "steer text")
        item = app.state.task_history.mail_item(session.id, identity)
        assert app.state.task_history.read_content(item.content)["metadata"] == {}


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


def test_spawn_does_not_copy_parent_renderer_capabilities(
    tmp_path: Path,
) -> None:
    """A child's renderer negotiation belongs to its own stable session."""
    import asyncio

    from tests.test_gact.task_records import record_assignment_fixture

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_StubChildAgent())
    with TestClient(app) as test_client:
        # Created through the route so the parent's agent is pinned at creation.
        parent_id = test_client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        record_assignment_fixture(app, parent_id)
        app.state.sessions.update(
            parent_id,
            metadata_patch={
                "a2ui_client_capabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}},
                "a2ui_client_data_model": {"surfaces": {"private": {"value": "parent only"}}},
            },
        )
        owner = app.state.conversations
        receipt = asyncio.run_coroutine_threadsafe(
            owner.spawn(
                owner.caller(parent_id), "Analyze the dataset", "clio/standard", "renderer-child"
            ),
            owner._loop,
        ).result(timeout=10)
        task = app.state.task_history.task_snapshot(receipt["handle"])
        child = app.state.sessions.get(task.owner.session_id)
        assert child is not None
        assert "a2ui_client_capabilities" not in child.metadata
        assert "a2ui_client_data_model" not in child.metadata
        assert task.description == "Analyze the dataset"


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
    assert post.status_code == 202, post.text
    assert post.json()["task_handle"]
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
    assert resp.status_code == 202, resp.text
    assert resp.json()["task_handle"]
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
    assert resp.status_code == 202, resp.text
    assert resp.json()["task_handle"]
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
# GET /v1/agents/resolve and /v1/sessions/{sid}/agent carry the catalogs   #
# --------------------------------------------------------------------------- #


def test_agent_resolve_route_carries_the_agents_catalogs(client: TestClient) -> None:
    listed = client.get("/v1/agents")
    assert listed.status_code == 200
    assert "clio/standard" in [row["ref"] for row in listed.json()["agents"]]

    resp = client.get("/v1/agents/resolve", params={"ref": "clio/standard"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["catalogs"] == [{"id": "clio-workspace", "plugin": "core", "path": ""}]
    assert resolution_catalog_ids(SimpleNamespace(catalogs=body["catalogs"])) == [WORKSPACE_ID]


def test_session_agent_route_carries_the_sessions_catalogs(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"/v1/sessions/{sid}/agent")
    assert resp.status_code == 200, resp.text
    catalogs = resp.json()["resolution"]["catalogs"]
    assert [row["id"] for row in catalogs] == ["clio-workspace"]


# --------------------------------------------------------------------------- #
# POST /v1/agent-tasks/{id}/steer: the SAME door guard, applied to the       #
# CHILD session (S3 adversarial review, item 11)                            #
# --------------------------------------------------------------------------- #


class _SlowChildAgent:
    """Stays "running" long enough for one or two steers to land before settling."""

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        time.sleep(4.0)
        return SimpleNamespace(answer=f"child did: {question[:20]}", selected_expert="")


@pytest.fixture
def running_child_client(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Keep the actual child and its owning application in one lifespan."""
    import asyncio

    from tests.test_gact.task_records import record_assignment_fixture

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowChildAgent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        record_assignment_fixture(app, parent)
        owner = app.state.conversations
        receipt = asyncio.run_coroutine_threadsafe(
            owner.spawn(
                owner.caller(parent), "Analyze the dataset", "clio/standard", "capabilities-child"
            ),
            owner._loop,
        ).result(timeout=10)
        yield client, receipt["handle"]


def test_steer_door_malformed_capabilities_422_and_not_remembered(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    resp = test_client.post(
        f"/v1/sessions/{app.state.task_history.task_snapshot(task_id).owner.session_id}/messages",
        json={
            "text": "hello child",
            "delivery": "steer",
            "metadata": {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": "nope"}}},
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "a2ui_client_capabilities_invalid"

    task = app.state.task_history.task_snapshot(task_id)
    child = app.state.sessions.get(task.owner.session_id)
    assert A2UI_CLIENT_CAPABILITIES_METADATA_KEY not in child.metadata
    reasons = app.state.a2ui_catalogs.session_reasons(task.owner.session_id)
    assert any(row["reason"] == "a2ui_client_capabilities_invalid" for row in reasons)


def test_steer_door_valid_capabilities_remembered_on_child_session(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    resp = test_client.post(
        f"/v1/sessions/{app.state.task_history.task_snapshot(task_id).owner.session_id}/messages",
        json={
            "text": "hello child",
            "delivery": "steer",
            "metadata": {
                "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}}
            },
        },
    )
    assert resp.status_code == 202, resp.text

    task = app.state.task_history.task_snapshot(task_id)
    child = app.state.sessions.get(task.owner.session_id)
    assert child.metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY] == {
        "v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}
    }


def test_steer_door_data_model_checked_against_the_childs_own_surfaces(
    running_child_client: tuple[TestClient, str],
) -> None:
    test_client, task_id = running_child_client
    app = test_client.app
    task = app.state.task_history.task_snapshot(task_id)
    child_sid = task.owner.session_id

    # No surface on the CHILD requested sendDataModel yet -> refused.
    resp = test_client.post(
        f"/v1/sessions/{app.state.task_history.task_snapshot(task_id).owner.session_id}/messages",
        json={
            "text": "hello child",
            "delivery": "steer",
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
        f"/v1/sessions/{app.state.task_history.task_snapshot(task_id).owner.session_id}/messages",
        json={
            "text": "hello again",
            "delivery": "steer",
            "metadata": {
                "a2uiClientDataModel": {"version": "v0.9.1", "surfaces": {"s1": {"x": 1}}}
            },
        },
    )
    assert resp2.status_code == 202, resp2.text


# --------------------------------------------------------------------------- #
# A plugin-shipped catalog end to end: a session whose agent carries the     #
# ``a2ui-minimal`` fixture plugin is visible everywhere a2ui_capabilities     #
# reaches (S3 adversarial review, item 7)                                    #
# --------------------------------------------------------------------------- #


def test_plugin_catalog_session_capabilities_route_lists_it(client: TestClient) -> None:
    sid = _create_session(client)
    bind_session_plugins(client.app, sid, MINIMAL_PLUGIN)

    resp = client.get(f"/v1/sessions/{sid}/a2ui/capabilities")
    assert resp.status_code == 200
    assert resp.json()["agent"]["v0.9"]["supportedCatalogIds"] == [
        WORKSPACE_ID,
        MINIMAL_CATALOG_ID,
    ]


def test_plugin_catalog_select_catalog_picks_it_when_client_lists_it_first(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="t")
    bind_session_plugins(app, session.id, MINIMAL_PLUGIN)

    caps = parse_client_capabilities(
        {
            "a2uiClientCapabilities": {
                "v0.9": {"supportedCatalogIds": [MINIMAL_CATALOG_ID, BASIC_ID]}
            }
        }
    )
    assert caps is not None
    remember_client_capabilities(app, session.id, caps)

    selection = select_catalog(app, session.id)
    assert selection.catalog_id == MINIMAL_CATALOG_ID


def test_plugin_catalog_session_agent_route_carries_it(client: TestClient) -> None:
    sid = _create_session(client)
    bind_session_plugins(client.app, sid, MINIMAL_PLUGIN)

    resp = client.get(f"/v1/sessions/{sid}/agent")
    assert resp.status_code == 200, resp.text
    resolution = resp.json()["resolution"]
    assert MINIMAL_PLUGIN in [row["id"] for row in resolution["plugins"]]
    assert [(row["id"], row["plugin"]) for row in resolution["catalogs"]] == [
        ("clio-workspace", "core"),
        ("minimal", MINIMAL_PLUGIN),
    ]
    assert resolution_catalog_ids(SimpleNamespace(catalogs=resolution["catalogs"])) == [
        WORKSPACE_ID,
        MINIMAL_CATALOG_ID,
    ]


def test_installed_plugin_route_carries_its_catalogs(client: TestClient) -> None:
    installed = client.post(
        "/v1/plugins/marketplaces",
        json={"source": str(A2UI_PLUGINS / MINIMAL_PLUGIN), "name": MINIMAL_PLUGIN},
    )
    assert installed.status_code == 201, installed.text

    plugins = client.get("/v1/plugins")
    assert plugins.status_code == 200, plugins.text
    [row] = [row for row in plugins.json()["plugins"] if row["id"] == MINIMAL_PLUGIN]
    # Exactly what the plugin ships (one catalog), never the builtins implicitly.
    assert row["catalogs"] == ["minimal"]

    resolved = client.get(
        "/v1/agents/resolve", params={"ref": "clio/standard", "add": MINIMAL_PLUGIN}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolution_catalog_ids(SimpleNamespace(catalogs=resolved.json()["catalogs"])) == [
        WORKSPACE_ID,
        MINIMAL_CATALOG_ID,
    ]
