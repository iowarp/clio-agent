"""Connector discovery: the agent offers a connector that is off (CD-1 to CD-6)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import secrets, state
from clio_agent.gact import connector_offer as offer
from clio_agent.gact import connector_setup
from clio_agent.gact import connectors_service as service

REFS = ("clio/standard",)


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=None, agent=None))


@pytest.fixture
def conversation(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A conversation whose agent is ``clio/standard``, with no window answering cards."""

    app = _app()
    shipped = state.all_entries
    monkeypatch.setattr(  # the fixture catalog only, not the connectors CLIO ships
        state, "all_entries", lambda: {k: v for k, v in shipped().items() if k.startswith("fix")}
    )
    monkeypatch.setattr(offer, "session_agent", lambda _a, _s: (REFS, REFS[0], "Standard"))
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


def test_offerable_names_the_connectors_that_do_not_reach_the_agent(conversation: Any) -> None:
    names = [found.id for found in offer.offerable(conversation, "sess_a")]
    assert names == ["fixture-plain", "fixture-secret", "fixture-signin"]
    state.update("fixture-plain", enabled=True, scope="all")
    state.update("fixture-secret", enabled=True, scope={"agents": ["someone/else"]})
    names = [found.id for found in offer.offerable(conversation, "sess_a")]
    assert names == ["fixture-secret", "fixture-signin"]  # scoped elsewhere: still offerable
    facts, total = offer.offerable_facts(conversation, "sess_a")
    assert total == 2 and facts[0] == {
        "id": "fixture-secret",
        "name": "Fixture Secret",
        "description": "A test connector that needs an access token and brings a plugin",
    }
    assert offer.offerable_facts(conversation, "") == ([], 0)


def test_the_facts_are_capped_and_say_so(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent import injections

    monkeypatch.setenv("CLIO_CONNECTORS_OFFER_LISTED", "1")
    facts, total = offer.offerable_facts(conversation, "sess_a")
    assert len(facts) == 1 and total == 3
    text = injections.render(
        "session_facts_connectors", {"connectors": facts, "total": total}, app=None, session_id=""
    )
    assert "Fixture Plain (fixture-plain): A test connector with nothing to set up" in text
    assert "(3, 1 shown)" in text and "with no connector lists them all" in text
    assert "offer_connector" in text and "search_tools" in text
    monkeypatch.setenv("CLIO_CONNECTORS_OFFER_LISTED", "-1")
    with pytest.raises(ValueError):
        offer.offer_listed()


def test_turn_on_enables_for_this_agent_and_the_tools_join_the_turn(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled: list[tuple[str, Any]] = []

    async def enable(_app: Any, connector_id: str, scope: Any) -> dict:
        enabled.append((connector_id, scope))
        state.update(connector_id, enabled=True, scope=scope)
        return {}

    monkeypatch.setattr(service, "enable", enable)
    monkeypatch.setattr(
        offer, "join_running_turn", lambda _app, cid: ([f"{cid}_one", f"{cid}_two"], "")
    )
    worker, outcome = _offer_in_thread(
        connector="Fixture Secret", reason="It checks the token for this request."
    )
    card = _pending_card(conversation, worker)
    assert card.kind == "connector_setup" and card.title == "Turn on Fixture Secret"
    payload = card.payload
    assert payload["intent"] == "enable" and payload["tool_name"] == "offer_connector"
    assert payload["reason"] == "It checks the token for this request."
    assert payload["agent_ref"] == "clio/standard" and payload["agent_name"] == "Standard"
    assert [row["key"] for row in payload["missing"]] == ["token", "where"]
    with pytest.raises(connector_setup.SetupResponseError) as incomplete:
        asyncio.run(connector_setup.respond(conversation, {"sess_a"}, card.id, "submit", {}, {}))
    assert incomplete.value.code == "connector_setup_incomplete" and not enabled
    with pytest.raises(connector_setup.SetupResponseError) as bad_scope:
        asyncio.run(
            connector_setup.respond(
                conversation, {"sess_a"}, card.id, "submit", {}, {"scope": "everywhere"}
            )
        )
    assert bad_scope.value.code == "connector_scope_invalid"
    answered = asyncio.run(
        connector_setup.respond(
            conversation,
            {"sess_a"},
            card.id,
            "submit",
            {"token": "zz-typed-zz", "where": "node08"},
            {"scope": "agent"},
        )
    )
    worker.join(5)
    assert answered.status == "answered"
    assert enabled == [("fixture-secret", {"agents": ["clio/standard"]})]
    assert secrets.values("fixture-secret")["token"] == "zz-typed-zz"
    result = outcome["value"]
    assert result["enabled"] is True and result["already_on"] is False
    assert result["tools"] == ["fixture-secret_one", "fixture-secret_two"]
    assert result["scope"] == {"agents": ["clio/standard"]}
    assert "turned on Fixture Secret for this conversation's agent" in result["message"]
    assert "zz-typed-zz" not in repr(result) + repr(conversation.state.connector_setups)


def test_not_now_is_a_typed_refusal_and_changes_nothing(conversation: Any) -> None:
    worker, outcome = _offer_in_thread(connector="fixture-plain", reason="To try it.")
    card = _pending_card(conversation, worker)
    assert [row["key"] for row in card.payload["missing"]] == []
    answered = asyncio.run(
        connector_setup.respond(conversation, {"sess_a"}, card.id, "decline", {}, {})
    )
    worker.join(5)
    assert answered.status == "denied"
    assert outcome["value"]["error"] == offer.OFFER_DECLINED
    assert "Fixture Plain" in outcome["value"]["message"]
    assert not state.choice("fixture-plain").enabled


def test_a_sign_in_is_asked_by_the_first_call_not_the_offer(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def enable(_app: Any, connector_id: str, scope: Any) -> dict:
        state.update(connector_id, enabled=True, scope=scope)
        return {}

    monkeypatch.setattr(service, "enable", enable)
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
            {"scope": "all"},
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


def test_listing_unknown_and_already_on(conversation: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = asyncio.run(offer.offer_connector())
    assert [row["id"] for row in listed["offerable"]] == [
        "fixture-plain",
        "fixture-secret",
        "fixture-signin",
    ]
    missing = asyncio.run(offer.offer_connector(connector="nope"))
    assert missing["error"] == "connector_not_found" and len(missing["offerable"]) == 3
    state.update("fixture-plain", enabled=True, scope="all")
    monkeypatch.setattr(offer, "join_running_turn", lambda _app, cid: ([f"{cid}_one"], ""))
    on = asyncio.run(offer.offer_connector(connector="fixture-plain"))
    assert on["already_on"] is True and on["tools"] == ["fixture-plain_one"]
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

    def join(_agent: Any, got: Any, namespace: str, declaration: Any) -> dict:
        assert got is executor and declaration == {"command": "x"}
        got.table[f"{namespace}_ping"] = object()
        return {f"{namespace}_ping": object()}

    monkeypatch.setattr(connector_rebind, "join_namespace", join)
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
        {"connector": "paraview", "reason": "Show it in 3D."},
        None,
        {
            "connector_name": "A Very Long Connector Name For Scientific Data Views",
            "reason": "Show it in 3D.",
            "scope": "all",
            "state": {"status": "ready"},
            "tools": [f"viewer_tool_{n}" for n in range(30)],
            "message": "The person turned it on.",
        },
    )
    assert turned_on["action"] == "Offer" and "status" not in turned_on
    texts = {block["id"]: block["text"] for block in turned_on["blocks"]}
    assert texts["reason"] == "Show it in 3D." and texts["outcome"] == "Turned on"
    assert texts["scope"] == "Every conversation" and texts["tools"].count("\n") == 29
    declined = offer.present(
        {"connector": "x"}, None, {"error": offer.OFFER_DECLINED, "connector_name": "X"}
    )
    assert declined["status"] == "denied" and declined["blocks"][-1]["text"] == "Not now"
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
            "scope": {"agents": ["clio/standard"]},
            "state": {"status": "unreachable", "reason": "Nothing answered"},
            "tools": [],
            "join_reason": "connector_join_listing_failed",
            "already_on": True,
        },
    )
    texts = {block["id"]: block["text"] for block in scoped["blocks"]}
    assert texts["outcome"] == "Already on" and texts["scope"] == "Only clio/standard"
    assert texts["tools"] == "connector_join_listing_failed" and texts["state"]
