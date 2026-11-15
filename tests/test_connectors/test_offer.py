"""Connector discovery: the agent offers to add a connector to this conversation (CD-D1 to
CD-D8, CN10 *Amended*)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import secrets, state
from clio_agent.gact import connector_follow, connector_setup
from clio_agent.gact import connector_offer as offer
from clio_agent.gact import connectors_service as service

REFS = ("clio/standard",)


def _app() -> Any:
    from tests.test_connectors.conftest import FakeSessions

    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=FakeSessions(), agent=None))


@pytest.fixture
def conversation(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A conversation whose agent is ``clio/standard``, with no window answering cards.

    ``app.state.here`` holds the connectors whose plugin the conversation has; adding one
    to the conversation records it there.
    """

    app = _app()
    app.state.here = {}
    app.state.added = []
    shipped = state.all_entries
    monkeypatch.setattr(  # the fixture catalog only, not the connectors CLIO ships
        state, "all_entries", lambda: {k: v for k, v in shipped().items() if k.startswith("fix")}
    )
    monkeypatch.setattr(offer, "session_agent", lambda _a, _s: (REFS, REFS[0], "Standard"))
    monkeypatch.setattr(offer, "conversation_connectors", lambda a, _s: dict(a.state.here))

    def add(a: Any, sid: str, cid: str) -> list[str]:
        a.state.added.append((sid, cid))
        a.state.here[cid] = [f"connector-{cid}"]
        return [f"connector-{cid}"]

    monkeypatch.setattr(connector_follow, "add_to_conversation", add)
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda _app, sid: sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: None
    )
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(offer, "_session_id", lambda: "sess_a")
    return app


def _offer_in_thread(**kwargs: Any) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["value"] = asyncio.run(offer.offer_connector(**kwargs))

    worker = threading.Thread(target=run)
    worker.start()
    return worker, outcome


def _pending_card(app: Any, worker: threading.Thread) -> Any:
    for _ in range(200):
        rows = getattr(app.state, "connector_setups", None) or {}
        if any(row["status"] == "pending" for row in rows.values()):
            break
        worker.join(0.05)
    return next(c for c in connector_setup.projected(app, {"sess_a"}) if c.status == "pending")


def _enable_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    enabled: list[str] = []

    async def enable(_app: Any, connector_id: str) -> dict:
        enabled.append(connector_id)
        state.update(connector_id, enabled=True)
        return {}

    monkeypatch.setattr(service, "enable", enable)
    return enabled


def test_offerable_names_every_connector_not_in_this_conversation(conversation: Any) -> None:
    names = [found.id for found in offer.offerable(conversation, "sess_a")]
    assert names == ["fixture-plain", "fixture-secret", "fixture-signin"]
    state.update("fixture-plain", enabled=True)
    state.update("fixture-secret", enabled=True)
    conversation.state.here = {"fixture-plain": ["connector-fixture-plain"]}
    names = [found.id for found in offer.offerable(conversation, "sess_a")]
    assert names == ["fixture-secret", "fixture-signin"]  # on but not here: still offerable
    assert [found.id for found in offer.offerable(conversation, "sess_a")] == [
        "fixture-secret",
        "fixture-signin",
    ]


def test_offerable_is_empty_when_the_conversation_cannot_resolve(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents.resolution import SessionAgentUnavailable

    app = _app()
    assert offer.conversation_connectors(app, "sess_a") is None
    app.state.sessions = {"sess_a": SimpleNamespace(metadata={})}

    def broken(_app: Any, _sid: str) -> Any:
        raise SessionAgentUnavailable("x", "agent_not_installed", [])

    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_resolution", broken)
    assert offer.offerable(app, "sess_a") == []
    pin = SimpleNamespace(
        plugins=[{"id": "p", "follows": "fixture-plain"}, {"id": "q", "follows": ""}]
    )
    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_resolution", lambda _a, _s: pin)
    assert offer.conversation_connectors(app, "sess_a") == {"fixture-plain": ["p"]}


def test_add_turns_it_on_adds_it_here_and_the_tools_join_the_turn(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = _enable_spy(monkeypatch)
    monkeypatch.setattr(
        offer, "join_running_turn", lambda _app, cid: ([f"{cid}_one", f"{cid}_two"], "")
    )
    worker, outcome = _offer_in_thread(
        connector="Fixture Secret", reason="It checks the token for this request."
    )
    card = _pending_card(conversation, worker)
    assert card.kind == "connector_setup"
    assert card.title == "Add Fixture Secret to this conversation"
    payload = card.payload
    assert payload["intent"] == "enable" and payload["tool_name"] == "offer_plugin"
    assert payload["reason"] == "It checks the token for this request."
    assert payload["connector_enabled"] is False
    assert [row["key"] for row in payload["missing"]] == ["token", "where"]
    with pytest.raises(connector_setup.SetupResponseError) as incomplete:
        asyncio.run(connector_setup.respond(conversation, {"sess_a"}, card.id, "submit", {}, {}))
    assert incomplete.value.code == "connector_setup_incomplete" and not enabled
    answered = asyncio.run(
        connector_setup.respond(
            conversation,
            {"sess_a"},
            card.id,
            "submit",
            {"token": "zz-typed-zz", "where": "node08"},
            {},
        )
    )
    worker.join(5)
    assert answered.status == "answered"
    assert enabled == ["fixture-secret"]
    assert conversation.state.added == [("sess_a", "fixture-secret")]
    assert secrets.values("fixture-secret")["token"] == "zz-typed-zz"
    # CV-D5: a value typed in the card is this conversation's, not the default.
    assert state.choice("fixture-secret").values == {}
    assert conversation.state.sessions.get("sess_a").metadata["connector_values"] == {
        "fixture-secret": {"where": "node08"}
    }
    result = outcome["value"]
    assert result["enabled"] is True and result["enabled_now"] is True
    assert result["already_on"] is False and "scope" not in result
    assert result["tools"] == ["fixture-secret_one", "fixture-secret_two"]
    assert result["plugins"] == ["connector-fixture-secret"]
    assert "turned on Fixture Secret and added it to this conversation" in result["message"]
    assert "joined this conversation only" in result["message"]
    assert "zz-typed-zz" not in repr(result) + repr(conversation.state.connector_setups)


def test_a_connector_that_is_on_is_only_added_here(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = _enable_spy(monkeypatch)
    state.update("fixture-plain", enabled=True)
    monkeypatch.setattr(offer, "join_running_turn", lambda _app, cid: ([f"{cid}_one"], ""))
    worker, outcome = _offer_in_thread(connector="fixture-plain", reason="View it.")
    card = _pending_card(conversation, worker)
    assert card.payload["connector_enabled"] is True
    asyncio.run(connector_setup.respond(conversation, {"sess_a"}, card.id, "submit", {}, {}))
    worker.join(5)
    result = outcome["value"]
    assert enabled == [] and result["enabled_now"] is False
    assert result["message"].startswith("The person added Fixture Plain to this conversation")
    assert conversation.state.added == [("sess_a", "fixture-plain")]


def test_a_conversation_that_cannot_take_the_plugin_is_a_typed_answer(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.update("fixture-plain", enabled=True)

    def refuse(_a: Any, _sid: str, _cid: str) -> list[str]:
        raise connector_follow.ConnectorAddError("subagent_plugins_fixed", "fixed")

    monkeypatch.setattr(connector_follow, "add_to_conversation", refuse)
    worker, _outcome = _offer_in_thread(connector="fixture-plain")
    card = _pending_card(conversation, worker)
    with pytest.raises(connector_setup.SetupResponseError) as refused:
        asyncio.run(connector_setup.respond(conversation, {"sess_a"}, card.id, "submit", {}, {}))
    assert refused.value.code == "subagent_plugins_fixed"
    asyncio.run(connector_setup.respond(conversation, {"sess_a"}, card.id, "decline", {}, {}))
    worker.join(5)


def test_not_now_is_a_typed_refusal_and_changes_nothing(conversation: Any) -> None:
    worker, outcome = _offer_in_thread(connector="fixture-plain", reason="To try it.")
    card = _pending_card(conversation, worker)
    assert [row["key"] for row in card.payload["missing"]] == []
    answered = asyncio.run(
        connector_setup.respond(conversation, {"sess_a"}, card.id, "decline", {}, {})
    )
    worker.join(5)
    assert answered.status == "denied"
    assert outcome["value"]["refusal"] == offer.OFFER_DECLINED and "error" not in outcome["value"]
    assert "Fixture Plain" in outcome["value"]["message"]
    assert not state.choice("fixture-plain").enabled and conversation.state.added == []


def test_a_sign_in_is_asked_by_the_first_call_not_the_offer(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_spy(monkeypatch)
    monkeypatch.setattr(offer, "join_running_turn", lambda _app, cid: ([], "connector_join_x"))
    worker, outcome = _offer_in_thread(connector="fixture-signin")
    card = _pending_card(conversation, worker)
    assert [row["key"] for row in card.payload["missing"]] == ["address"]
    assert card.payload["asks_sign_in"] is True
    asyncio.run(
        connector_setup.respond(
            conversation,
            {"sess_a"},
            card.id,
            "submit",
            {"address": "http://127.0.0.1:9/mcp"},
            {},
        )
    )
    worker.join(5)
    result = outcome["value"]
    assert result["tools"] == [] and result["join_reason"] == "connector_join_x"
    assert "could not join this turn (connector_join_x)" in result["message"]


def test_stopping_the_turn_ends_the_offer(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: stop
    )
    result = asyncio.run(offer.offer_connector(connector="fixture-plain"))
    assert result["error"] == offer.OFFER_CANCELLED and result["outcome"] == "cancelled"


def test_listing_unknown_and_already_here(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = asyncio.run(offer.offer_connector())
    assert [row["id"] for row in listed["offerable"]] == [
        "fixture-plain",
        "fixture-secret",
        "fixture-signin",
    ]
    assert "can add to this conversation" in listed["message"]
    missing = asyncio.run(offer.offer_connector(connector="nope"))
    assert missing["error"] == "connector_not_found" and len(missing["offerable"]) == 3
    state.update("fixture-plain", enabled=True)
    conversation.state.here = {"fixture-plain": ["connector-fixture-plain"]}
    monkeypatch.setattr(offer, "join_running_turn", lambda _app, cid: ([f"{cid}_one"], ""))
    on = asyncio.run(offer.offer_connector(connector="fixture-plain"))
    assert on["already_on"] is True and on["tools"] == ["fixture-plain_one"]
    assert on["message"].startswith("Fixture Plain is already in this conversation")
    assert not getattr(conversation.state, "connector_setups", None)
    monkeypatch.setattr(offer, "_session_id", lambda: "")
    assert asyncio.run(offer.offer_connector(connector="x"))["error"] == (
        "connector_offer_no_conversation"
    )


def test_join_running_turn_mounts_the_namespace_and_the_next_step_lists_it(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_rebind
    from clio_agent.gact.agents import clio_react
    from clio_agent.gact.agents.live_tool_refresh import LiveToolRefresher
    from clio_agent.gact.mcp_app_tools import AppToolRefresher

    class Executor:
        tools_revision = 0

        def __init__(self) -> None:
            self.table: dict[str, Any] = {}

        def to_dspy_tools(self) -> list[Any]:
            return [SimpleNamespace(name=name) for name in self.table]

    executor = Executor()
    refresher = AppToolRefresher(None)
    program = SimpleNamespace(_clio_tool_refresh=refresher)
    monkeypatch.setattr(clio_react, "active_loop", lambda: SimpleNamespace(agent=program))
    monkeypatch.setattr(service, "saved_connection", lambda cid: {"command": "x"})
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_base_agent_tool_executor", lambda _a: executor
    )

    def join(_agent: Any, got: Any, namespace: str, declaration: Any, *, spec: Any) -> dict:
        assert got is executor and declaration == {"command": "x"} and spec is None
        got.table[f"{namespace}_ping"] = object()
        return {f"{namespace}_ping": object()}

    monkeypatch.setattr(connector_rebind, "join_namespace", join)
    monkeypatch.setattr(connector_rebind, "conversation_spec", lambda *_a: None)
    tools, reason = offer.join_running_turn(_app(), "fixture-plain")
    assert (tools, reason) == (["fixture-plain_ping"], "")
    assert isinstance(refresher.inner, LiveToolRefresher)
    monkeypatch.setattr(
        "clio_agent.gact.agents.live_tool_refresh._wrap_for_agent", lambda tools: tools
    )
    refresher.inner._wrap = lambda tools: tools
    listed: dict[str, Any] = {"submit": object()}
    assert refresher.inner(listed) is True
    assert list(listed) == ["fixture-plain_ping", "submit"]
    monkeypatch.setattr(clio_react, "active_loop", lambda: None)
    assert offer.join_running_turn(_app(), "fixture-plain") == ([], "connector_join_no_turn")


def test_join_namespace_merges_the_enable_listing(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.types import Tool

    from clio_agent.connectors import listing
    from clio_agent.gact import connector_rebind

    installed: list[str] = []
    monkeypatch.setattr(
        connector_rebind, "_install", lambda _e, ns, spec, root: installed.append(ns)
    )
    listing.remember("fixture-plain", [Tool(name="ping", inputSchema={"type": "object"})])
    merged: dict[str, Any] = {}

    class Executor:
        _clio_namespace_specs: dict[str, Any] = {}

        def merge_namespace_tools(self, namespace: str, tools: dict) -> None:
            merged.update(tools)

    declaration = {"command": "python3", "args": ["-c", "pass"], "connector": "fixture-plain"}
    tools = connector_rebind.join_namespace(None, Executor(), "fixture-plain", declaration)
    assert list(tools) == ["fixture-plain_ping"] and list(merged) == ["fixture-plain_ping"]
    assert installed == ["fixture-plain"]
    with pytest.raises(connector_rebind.ConnectorJoinError) as no_fleet:
        connector_rebind.join_namespace(None, object(), "fixture-plain", declaration)
    assert no_fleet.value.reason == "connector_join_fleet_unavailable"


def test_the_row_reads_offer_and_says_what_the_person_chose() -> None:
    turned_on = offer.present(
        {"connector": "fixture-viewer", "reason": "Show it in 3D."},
        None,
        {
            "connector_name": "A Very Long Connector Name For Scientific Data Views",
            "reason": "Show it in 3D.",
            "enabled_now": True,
            "state": {"status": "ready"},
            "tools": [f"viewer_tool_{n}" for n in range(30)],
            "message": "The person turned it on.",
        },
    )
    assert turned_on["action"] == "Offer" and "status" not in turned_on
    assert turned_on["blocks"][0] == {
        "id": "connector",
        "type": "text",
        "text": "A Very Long Connector Name For Scientific Data Views",
    }
    started = offer.present_start({"connector": "fixture-viewer", "reason": "Show it in 3D."})
    assert started["subject"] == "connector" and started["blocks"][1]["text"] == "Show it in 3D."
    assert offer.present_start({})["action"] == "List"
    texts = {block["id"]: block["text"] for block in turned_on["blocks"]}
    assert texts["reason"] == "Show it in 3D." and texts["outcome"] == "Turned on and added"
    assert texts["scope"] == "This conversation" and texts["tools"].count("\n") == 29
    declined = offer.present(
        {"connector": "x"}, None, {"refusal": offer.OFFER_DECLINED, "connector_name": "X"}
    )
    assert "status" not in declined and declined["blocks"][-1]["text"] == "Not now"
    assert declined["subject"] == "connector" and declined["blocks"][0]["text"] == "X"
    cancelled = offer.present(
        {"connector": "x"},
        None,
        {"error": offer.OFFER_CANCELLED, "outcome": "cancelled", "message": "stopped"},
    )
    assert cancelled["status"] == "cancelled"
    unknown = offer.present(
        {"connector": "nope"},
        None,
        {"error": "connector_not_found", "message": "No connector", "offerable": [{"name": "A"}]},
    )
    assert unknown["status"] == "failed" and unknown["blocks"][-1]["text"] == "A"
    listed = offer.present({}, None, {"offerable": [], "message": "0 connectors"})
    assert listed["action"] == "List" and listed["blocks"][-1]["text"] == "None"
    scoped = offer.present(
        {"connector": "x"},
        None,
        {
            "connector_name": "X",
            "state": {"status": "unreachable", "reason": "Nothing answered"},
            "tools": [],
            "join_reason": "connector_join_listing_failed",
            "already_on": True,
        },
    )
    texts = {block["id"]: block["text"] for block in scoped["blocks"]}
    assert texts["outcome"] == "Already here" and texts["scope"] == "This conversation"
    added = offer.present({"connector": "x"}, None, {"connector_name": "X", "tools": ["a"]})
    assert {b["id"]: b["text"] for b in added["blocks"]}["outcome"] == "Added"
    assert texts["tools"] == "connector_join_listing_failed" and texts["state"]
