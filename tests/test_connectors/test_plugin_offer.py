"""Plugin discovery: the agent offers any plugin of the person's library (CF-D3 to CF-D7)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import state
from clio_agent.gact import connector_follow, connector_setup, plugin_offer
from clio_agent.gact import connector_offer as connectors
from clio_agent.gact.plugin_offer_presentation import present, present_start
from clio_agent.plugins.records import PluginRecord, SkillRecord, ToolRecord
from clio_agent.plugins.store import BUILTIN_MARKETPLACE

TARIFF_TEXT = (
    "Landed cost of imported goods: duty rates by HS code and origin, VAT on the customs value, "
    "and what a shipment costs once it has cleared customs."
)


def _record(rid: str, description: str = "", **fields: Any) -> PluginRecord:
    return PluginRecord(
        id=rid,
        version="1.0.0",
        description=description or f"The {rid} plugin.",
        kind=fields.pop("kind", "plugin"),
        format="agent-plugins",
        root=f"/plugins/{rid}",
        marketplace=fields.pop("marketplace", "lab"),
        **fields,
    )


TARIFF = _record(
    "tariff-calc",
    TARIFF_TEXT,
    skills=(SkillRecord("tariff-landed-cost", "Compute a landed cost", "/x/SKILL.md"),),
    mcp_servers={"tariff": {"command": "python3"}},
)
RECORDS = [
    _record("core", marketplace=BUILTIN_MARKETPLACE),
    _record("paraview", follows="fixture-plain"),
    _record("bundle", kind="pack"),
    _record("python-tools", tools=(ToolRecord("pt_go", "python"),), python_pending=True),
    _record("already-here"),
    _record("Alpha"),
    TARIFF,
]


@pytest.fixture
def app(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A conversation that has ``already-here``; the library is :data:`RECORDS`."""

    from tests.test_connectors.conftest import FakeSessions

    store = SimpleNamespace(
        records=lambda: list(RECORDS),
        trusted=lambda record: record.id != "python-tools",
        marketplaces=lambda: {BUILTIN_MARKETPLACE: {}, "lab": {}},
    )
    the_app = SimpleNamespace(
        state=SimpleNamespace(
            bus=None,
            sessions=FakeSessions(),
            agent=None,
            plugin_service=SimpleNamespace(store=store),
        )
    )
    the_app.state.here = {"already-here"}
    the_app.state.added = []
    shipped = state.all_entries
    monkeypatch.setattr(
        state, "all_entries", lambda: {k: v for k, v in shipped().items() if k.startswith("fix")}
    )

    def resolution(a: Any, _sid: str) -> Any:
        rows = [{"id": pid} for pid in sorted(a.state.here)]
        servers = {"tariff": {"plugin": "tariff-calc"}} if "tariff-calc" in a.state.here else {}
        return SimpleNamespace(plugins=rows, mcp_servers=servers, skills=[])

    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_resolution", resolution)
    monkeypatch.setattr(connectors, "conversation_connectors", lambda _a, _s: {})
    monkeypatch.setattr(connectors, "session_agent", lambda _a, _s: ((), "clio/standard", "Std"))

    def add(a: Any, sid: str, refs: list[str]) -> Any:
        a.state.added.append((sid, refs))
        a.state.here.add(refs[0].split("/")[-1])
        return resolution(a, sid)

    monkeypatch.setattr(connector_follow, "add_plugins", add)
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda _app, sid: sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: None
    )
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: the_app)
    monkeypatch.setattr(connectors, "_session_id", lambda: "sess_a")
    return the_app


def _offer_in_thread(**kwargs: Any) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["value"] = asyncio.run(plugin_offer.offer_plugin(**kwargs))

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


# ------------------------------------------------------------- what is offered


def test_the_list_has_connectors_then_library_plugins_not_here(app: Any) -> None:
    rows = plugin_offer.offerable(app, "sess_a")
    assert [(row["kind"], row["id"]) for row in rows] == [
        ("connector", "fixture-plain"),
        ("connector", "fixture-secret"),
        ("connector", "fixture-signin"),
        ("plugin", "Alpha"),
        ("plugin", "tariff-calc"),
    ]
    tariff = rows[-1]
    assert tariff["ref"] == "lab/tariff-calc" and tariff["summary"].endswith("…")
    assert len(tariff["summary"]) <= plugin_offer.SUMMARY_CHARS
    assert plugin_offer.summary("short\n text") == "short text"


def test_the_facts_are_capped_say_so_and_cost_little(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent import injections

    rows, total = plugin_offer.offerable_facts(app, "sess_a")
    text = injections.render(
        "session_facts_plugins", {"plugins": rows, "total": total}, app=None, session_id=""
    )
    assert "- Fixture Plain (fixture-plain, connector, off): A test connector" in text
    assert "- tariff-calc [lab]: Landed cost of imported goods" in text
    assert "offer_plugin asks them" in text and "with no plugin lists them all" not in text
    assert len(text) < 1200  # about 300 tokens for five rows (CF-D4)
    monkeypatch.setenv("CLIO_PLUGINS_OFFER_LISTED", "2")
    rows, total = plugin_offer.offerable_facts(app, "sess_a")
    text = injections.render(
        "session_facts_plugins", {"plugins": rows, "total": total}, app=None, session_id=""
    )
    assert "(5, 2 shown)" in text and "offer_plugin with no plugin lists them all" in text
    assert plugin_offer.offerable_facts(app, "") == ([], 0)
    monkeypatch.setenv("CLIO_PLUGINS_OFFER_LISTED", "-1")
    with pytest.raises(ValueError):
        plugin_offer.offer_listed()


def test_a_second_marketplace_is_offered_and_a_removed_one_is_not(app: Any) -> None:
    store = app.state.plugin_service.store
    science = _record("spectra", marketplace="science")
    records = [*RECORDS, science]
    store.records = lambda: list(records)
    store.marketplaces = lambda: {
        BUILTIN_MARKETPLACE: {},
        "lab": {"installed_at": "2026-01-01T00:00:00Z"},
        "science": {"installed_at": "2026-02-01T00:00:00Z"},
    }
    listed = plugin_offer.offerable(app, "sess_a")
    # The most recently added marketplace first, after the connectors.
    assert [row["id"] for row in listed if row["kind"] == "plugin"] == [
        "spectra",
        "Alpha",
        "tariff-calc",
    ]
    rows = {row["id"]: row for row in listed}
    assert rows["spectra"]["marketplace"] == "science" and rows["spectra"]["ref"] == (
        "science/spectra"
    )
    assert rows["tariff-calc"]["marketplace"] == "lab"
    # Removed: its records may linger in a stale view, but it is no longer added.
    store.marketplaces = lambda: {BUILTIN_MARKETPLACE: {}, "lab": {}}
    assert "spectra" not in {row["id"] for row in plugin_offer.offerable(app, "sess_a")}
    store.marketplaces = lambda: {BUILTIN_MARKETPLACE: {}, "science": {}}
    ids = {row["id"] for row in plugin_offer.offerable(app, "sess_a")}
    assert "spectra" in ids and "tariff-calc" not in ids and "Alpha" not in ids


def test_an_unresolvable_conversation_offers_nothing_typed(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents.resolution import SessionAgentUnavailable

    def broken(_a: Any, _s: str) -> Any:
        raise SessionAgentUnavailable("x", "agent_not_installed", [])

    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_resolution", broken)
    assert plugin_offer.offerable(app, "sess_a") == []


# ------------------------------------------------------------------ the card


def test_add_puts_the_plugin_here_and_its_skills_and_servers_join_the_turn(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    joined: list[tuple[str, bool]] = []

    def join(_app: Any, namespace: str, *, plugin_server: bool = False) -> tuple[list[str], str]:
        joined.append((namespace, plugin_server))
        return [f"{namespace}_landed_cost"], ""

    monkeypatch.setattr(connectors, "join_running_turn", join)
    worker, outcome = _offer_in_thread(plugin="tariff-calc", reason="It computes duty and VAT.")
    card = _pending_card(app, worker)
    assert card.kind == "connector_setup" and card.title == "Add tariff-calc to this conversation"
    payload = card.payload
    assert payload["intent"] == "add_plugin" and payload["tool_name"] == "offer_plugin"
    assert payload["plugin_id"] == "tariff-calc" and payload["plugin_ref"] == "lab/tariff-calc"
    assert payload["connector_id"] == "" and payload["logo_url"] is None
    assert payload["missing"] == [] and payload["reason"] == "It computes duty and VAT."
    answered = asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "submit", {}, {}))
    worker.join(5)
    assert answered.status == "answered"
    assert app.state.added == [("sess_a", ["lab/tariff-calc"])]
    assert joined == [("tariff", True)]
    result = outcome["value"]
    assert result["plugin"] == "tariff-calc" and result["already_here"] is False
    assert result["skills"] == [
        {"id": "tariff-landed-cost", "description": "Compute a landed cost"}
    ]
    assert result["tools"] == ["tariff_landed_cost"] and "join_reason" not in result
    assert "load with load_skill from your next step: tariff-landed-cost" in result["message"]
    assert result["reason"] == "It computes duty and VAT."
    # Asked again: already here, no second card.
    again = asyncio.run(plugin_offer.offer_plugin(plugin="tariff-calc", reason="Duty."))
    assert again["already_here"] is True and len(app.state.connector_setups) == 1


def test_python_tools_arrive_with_the_next_message_typed(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record("py", tools=(ToolRecord("py_run", "python"),))
    monkeypatch.setattr(connectors, "join_running_turn", lambda *_a, **_k: ([], ""))
    result = plugin_offer._joined(app, "sess_a", record, already=False)
    assert result["tools_next_message"] == ["py_run"]
    assert result["join_reason"] == plugin_offer.TOOLS_NEXT_MESSAGE
    assert "arrive with the next message" in result["message"]


def test_not_now_and_a_conversation_that_cannot_take_it_are_typed(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, outcome = _offer_in_thread(plugin="Alpha", reason="To try it.")
    card = _pending_card(app, worker)
    asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "decline", {}, {}))
    worker.join(5)
    assert outcome["value"]["refusal"] == plugin_offer.OFFER_DECLINED
    assert "error" not in outcome["value"] and app.state.added == []

    def refuse(_a: Any, _sid: str, _refs: list[str]) -> Any:
        raise connector_follow.ConnectorAddError("subagent_plugins_fixed", "fixed")

    monkeypatch.setattr(connector_follow, "add_plugins", refuse)
    worker, _outcome = _offer_in_thread(plugin="lab/Alpha", reason="To try it.")
    card = _pending_card(app, worker)
    with pytest.raises(connector_setup.SetupResponseError) as refused:
        asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "submit", {}, {}))
    assert refused.value.status == 422 and refused.value.code == "subagent_plugins_fixed"
    with pytest.raises(connector_setup.SetupResponseError):
        asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "maybe", {}, {}))
    asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "decline", {}, {}))
    worker.join(5)


def test_stopping_the_turn_ends_the_offer(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: stop
    )
    result = asyncio.run(plugin_offer.offer_plugin(plugin="Alpha", reason="To try it."))
    assert result["error"] == plugin_offer.OFFER_CANCELLED and result["outcome"] == "cancelled"


def test_a_connector_or_its_plugin_is_the_connector_offer(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def offer(connector: str = "", reason: str = "") -> dict[str, Any]:
        asked.append(connector)
        return {"connector": connector}

    monkeypatch.setattr(connectors, "offer_connector", offer)
    asyncio.run(plugin_offer.offer_plugin(plugin="Fixture Plain", reason="r"))
    asyncio.run(plugin_offer.offer_plugin(plugin="paraview", reason="r"))  # follows fixture-plain
    assert asked == ["fixture-plain", "fixture-plain"]


def test_listing_unknown_and_no_conversation(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = asyncio.run(plugin_offer.offer_plugin(reason="r"))
    assert len(listed["offerable"]) == 5 and "can add" in listed["message"]
    for name in ("nope", "core", "python-tools", "bundle"):
        missing = asyncio.run(plugin_offer.offer_plugin(plugin=name, reason="r"))
        assert missing["error"] == "plugin_not_found", name
    monkeypatch.setattr(connectors, "_session_id", lambda: "")
    assert asyncio.run(plugin_offer.offer_plugin(plugin="x", reason="r"))["error"] == (
        "plugin_offer_no_conversation"
    )


def test_join_running_turn_needs_a_declared_spec_for_a_plugin_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import connector_rebind
    from clio_agent.gact.agents import clio_react
    from clio_agent.gact.mcp_app_tools import AppToolRefresher

    executor = SimpleNamespace(to_dspy_tools=lambda: [], tools_revision=0)
    program = SimpleNamespace(_clio_tool_refresh=AppToolRefresher(None))
    monkeypatch.setattr(clio_react, "active_loop", lambda: SimpleNamespace(agent=program))
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_base_agent_tool_executor", lambda _a: executor
    )
    monkeypatch.setattr(connectors, "_session_id", lambda: "sess_a")
    monkeypatch.setattr(connector_rebind, "conversation_spec", lambda *_a: None)
    app = SimpleNamespace(state=SimpleNamespace(agent=None))
    assert connectors.join_running_turn(app, "tariff", plugin_server=True) == (
        [],
        "plugin_join_undeclared",
    )


# --------------------------------------------------------------- presentation


def test_the_row_reads_offer_and_says_what_joined() -> None:
    added = present(
        {"plugin": "tariff-calc", "reason": "Duty and VAT."},
        None,
        {
            "plugin": "tariff-calc",
            "plugin_name": "tariff-calc",
            "description": "Landed cost of imported goods.",
            "skills": [{"id": f"skill-{n}", "description": "x" * 200} for n in range(12)],
            "tools": [f"tariff_tool_{n}" for n in range(25)],
            "tools_next_message": ["py_run"],
            "message": "The person added it.",
        },
    )
    assert added["action"] == "Offer" and added["subject"] == "plugin" and "status" not in added
    texts = {block["id"]: block for block in added["blocks"]}
    assert texts["plugin"]["text"] == "tariff-calc" and texts["reason"]["text"] == "Duty and VAT."
    assert texts["what"] == {
        "id": "what",
        "type": "text",
        "label": "What it is",
        "text": "Landed cost of imported goods.",
    }
    assert texts["outcome"]["text"] == "Added" and texts["scope"]["text"] == "This conversation"
    assert texts["skills"]["label"] == "Skills (12)" and texts["tools"]["label"] == "Tools (25)"
    assert texts["later"]["severity"] == "warning" and texts["later"]["text"] == "py_run"
    here = present({"plugin": "p"}, None, {"plugin": "p", "already_here": True, "join_reason": "x"})
    assert {b["id"]: b["text"] for b in here["blocks"]}["outcome"] == "Already here"
    declined = present({"plugin": "p"}, None, {"refusal": plugin_offer.OFFER_DECLINED})
    assert declined["blocks"][-1]["text"] == "Not now" and "status" not in declined
    failed = present(
        {"plugin": "nope"},
        None,
        {"error": "plugin_not_found", "message": "No plugin", "offerable": [{"name": "A"}]},
    )
    assert failed["status"] == "failed" and failed["blocks"][-1]["text"] == "A"
    stopped = present({}, None, {"error": plugin_offer.OFFER_CANCELLED, "outcome": "cancelled"})
    assert stopped["status"] == "cancelled"
    listed = present(
        {},
        None,
        {
            "offerable": [
                {"kind": "connector", "name": "ParaView", "enabled": True, "summary": "3D"},
                {"kind": "plugin", "name": "tariff-calc", "summary": "Duty"},
            ],
            "message": "2",
        },
    )
    assert listed["action"] == "List"
    assert listed["blocks"][-1]["text"] == "ParaView (connector, on): 3D\ntariff-calc: Duty"
    tagged = present({}, None, {"offerable": [{"name": "t", "marketplace": "lab", "summary": "D"}]})
    assert tagged["blocks"][-1]["text"] == "t [lab]: D"
    assert present({}, None, {"offerable": []})["blocks"][-1]["text"] == "None"
    connector = present({"plugin": "fixture-plain"}, None, {"connector_name": "X", "tools": []})
    assert connector["subject"] == "connector"
    assert present_start({})["action"] == "List"
    started = present_start({"plugin": "tariff-calc", "reason": "Duty."})
    assert started["subject"] == "plugin" and started["blocks"][1]["text"] == "Duty."


# ------------------------------------------------------ skills in the same turn


def test_a_skill_the_conversation_gained_mid_turn_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents import skill_join
    from clio_agent.gact.agents.skill_runtime import SkillRuntime

    skill = tmp_path / "tariff-calc" / "skills" / "tariff-landed-cost"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: tariff-landed-cost\ndescription: Compute a landed cost\n---\nStep one.\n",
        encoding="utf-8",
    )
    pinned = SimpleNamespace(
        skills=[
            {"id": "tariff-landed-cost", "plugin": "tariff-calc", "path": str(skill / "SKILL.md")}
        ]
    )
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(skill_join._ctx, "active_app", lambda: app)
    monkeypatch.setattr(skill_join._ctx, "active_session_id", lambda: "sess_a")
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution.session_resolution", lambda _a, _s: pinned
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda _a, session_id="": tmp_path,
    )
    events: list[str] = []
    monkeypatch.setattr(skill_join.trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
    runtime = SkillRuntime()
    assert skill_join.joined_skill(runtime, "not-here") is None
    joined = skill_join.joined_skill(runtime, "tariff-landed-cost")
    assert joined is not None and "tariff-landed-cost" in runtime.resolved
    assert "skill_joined_turn skill=tariff-landed-cost plugin=tariff-calc" in " ".join(events)
    monkeypatch.setattr(skill_join._ctx, "active_session_id", lambda: "")
    assert skill_join.joined_skill(SkillRuntime(), "tariff-landed-cost") is None


def test_a_search_miss_says_what_is_not_searched() -> None:
    from clio_agent.gact.search_tools import search_result

    catalog = SimpleNamespace(
        __len__=lambda: 1, search=lambda q, n: ([], []), names=lambda: ["fork"]
    )

    class Catalog:
        def __len__(self) -> int:
            return 1

        def search(self, query: str, limit: int) -> tuple[list[Any], list[str]]:
            return [], []

        def names(self) -> list[str]:
            return ["fork"]

    del catalog
    text, structured = search_result(Catalog(), "paraview")  # type: ignore[arg-type]
    assert not structured["tools"]
    assert "Only this conversation's tools are searched" in text and "offer_plugin" in text


def test_a_connector_offer_names_the_skills_its_plugin_brings(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = SimpleNamespace(
        skills=[
            {"id": "scenes", "plugin": "paraview", "description": "Work with scenes"},
            {"id": "other", "plugin": "core", "description": "x"},
        ]
    )
    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_resolution", lambda *_: pinned)
    assert connectors._plugin_skills(app, ["paraview"]) == [
        {"id": "scenes", "description": "Work with scenes"}
    ]
    assert connectors._plugin_skills(app, []) == []
    shown = connectors.present(
        {"connector": "paraview"},
        None,
        {"connector_name": "ParaView", "tools": ["a"], "skills": [{"id": "scenes"}]},
    )
    assert {b["id"]: b["text"] for b in shown["blocks"]}["skills"] == "scenes"


def test_the_reason_is_required_by_the_schema() -> None:
    """CF-N4: an offer without a reason is a typed validation error the model corrects."""

    from clio_agent.gact.agents.dspy_native_tool import ToolArgumentError

    tool = plugin_offer.build_offer_plugin_tool()
    schema = tool.format_as_litellm_function_call()["function"]["parameters"]
    assert schema["required"] == ["reason"]
    with pytest.raises(ToolArgumentError, match="missing required argument reason"):
        tool._validate_and_parse_args(plugin="tariff-calc")
    with pytest.raises(ToolArgumentError, match="reason"):
        tool._validate_and_parse_args(plugin="tariff-calc", reason="")
    parsed = tool._validate_and_parse_args(plugin="tariff-calc", reason="Duty and VAT.")
    assert parsed["reason"] == "Duty and VAT."
