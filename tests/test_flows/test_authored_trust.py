"""Trust follows authorship at the moment a plugin Clio wrote is first used (B6, B7).

A child started with ``plugins`` carries them; a plugin Clio wrote that is not trusted
yet is asked about once (with its review), someone else's untrusted plugin is refused
with ``plugin_not_trusted``, and ``offer_plugin`` adds a plugin Clio wrote after the
same single ask. A text-only plugin needs no ask.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.test_flows.conftest import default_program, post, wait_until
from tests.test_flows.test_plugin_authoring import _files, _idle, _session, _trusted

TEXT_ONLY = {
    "plugin.json": json.dumps(
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "units-note",
            "version": "0.1.0",
            "description": "Says which units the lab uses.",
        }
    ),
    "skills/lab-units/SKILL.md": (
        "---\nname: lab-units\ndescription: Use when reporting temperatures.\n---\n"
        "Report temperatures in kelvin.\n"
    ),
}


def _script(client: TestClient) -> dict[str, Any]:
    from clio_agent.gact.plugin_authoring import make_plugin
    from clio_agent.gact.plugin_offer import offer_plugin
    from clio_agent.gact.spawn_tools import spawn_child
    from clio_agent.gact.task_declarations import _runtime

    done: dict[str, Any] = {}

    def spawn(plugins: dict[str, list[str]]) -> dict[str, Any]:
        app, caller = _runtime()
        return asyncio.run(
            spawn_child(app, caller, "", "count the votes", f"inv-{len(done)}", plugins=plugins)
        )

    def program(question: str = "", **kwargs: Any) -> Any:
        import dspy

        if question == "make tally":
            done[question] = asyncio.run(make_plugin("tally", _files("0.1.0")))
        elif question == "make units-note":
            done[question] = asyncio.run(make_plugin("units-note", TEXT_ONLY))
        elif question.startswith("spawn with "):
            done[question] = spawn({"add": [question.removeprefix("spawn with ")]})
        elif question.startswith("offer "):
            plugin = question.removeprefix("offer ")
            done[question] = asyncio.run(offer_plugin("to count votes", plugin=plugin))
        else:
            return default_program(question=question, **kwargs)
        return dspy.Prediction(answer="done")

    client.app.state.flow_program["run"] = program
    return done


def _ask(client: TestClient, sid: str, done: dict[str, Any], question: str) -> dict[str, Any]:
    _idle(client, sid)
    post(client, sid, question)
    return wait_until(lambda: done.get(question), timeout=60)


def _pending(client: TestClient, tool: str) -> dict[str, Any]:
    def find() -> dict[str, Any] | None:
        rows = client.get("/v1/permissions").json()["permissions"]
        return next(
            (r for r in rows if r["tool_call"]["tool_name"] == tool and r["status"] == "pending"),
            None,
        )

    return wait_until(find, timeout=60)


def _child_plugins(client: TestClient, handle: str) -> list[str]:
    task = client.app.state.task_history.task_snapshot(handle)
    view = client.get(f"/v1/sessions/{task.owner.session_id}/agent").json()
    return [row["id"] for row in view["resolution"]["plugins"]]


def test_a_child_takes_a_plugin_clio_wrote_after_one_ask(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _ask(flow_app, sid, done, "make tally")
    post(flow_app, sid, "spawn with private-local/tally")
    row = _pending(flow_app, "spawn_agent_task")
    asked = row["tool_call"]["input"]
    assert asked["trust_kind"] == "add_plugin" and asked["where"] == "a child"
    assert asked["authored_by_clio"] is True and asked["plugin"] == "tally"
    assert asked["review_url"] == "/v1/plugins/tally/review"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
    started = wait_until(lambda: done.get("spawn with private-local/tally"), timeout=60)
    assert started.get("handle"), started
    assert _trusted(flow_app)
    assert "tally" in _child_plugins(flow_app, started["handle"])
    # The parent's own agent is unchanged.
    parent = flow_app.get(f"/v1/sessions/{sid}/agent").json()
    assert "tally" not in [r["id"] for r in parent["resolution"]["plugins"]]


def test_denying_the_ask_starts_no_child(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _ask(flow_app, sid, done, "make tally")
    post(flow_app, sid, "spawn with private-local/tally")
    row = _pending(flow_app, "spawn_agent_task")
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
    refused = wait_until(lambda: done.get("spawn with private-local/tally"), timeout=60)
    assert refused["accepted"] is False and refused["reason"] == "plugin_not_trusted"
    assert "handle" not in refused and not _trusted(flow_app)


def test_someone_elses_untrusted_plugin_is_refused_even_under_bypass(
    flow_app: TestClient, tmp_path: Path
) -> None:
    done = _script(flow_app)
    folder = tmp_path / "votes"
    for path, text in _files().items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text)
    flow_app.app.state.plugin_service.store.install(str(folder), name="votes")
    sid = _session(flow_app, "bypass")
    refused = _ask(flow_app, sid, done, "spawn with votes/tally")
    assert refused["accepted"] is False and refused["reason"] == "plugin_not_trusted"
    assert "Settings" in refused["error"]


def test_an_unknown_plugin_is_refused_typed(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    refused = _ask(flow_app, sid, done, "spawn with private-local/nothing")
    assert refused["accepted"] is False and refused["reason"] == "plugin_not_installed"


def test_a_text_only_plugin_reaches_a_child_without_an_ask(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    made = _ask(flow_app, sid, done, "make units-note")
    assert made["installed"] and made["executable"] is False and made["trusted"] is True
    started = _ask(flow_app, sid, done, "spawn with private-local/units-note")
    assert started.get("handle"), started
    assert "units-note" in _child_plugins(flow_app, started["handle"])


def test_offering_a_plugin_clio_wrote_asks_once_then_adds_it(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _ask(flow_app, sid, done, "make tally")
    post(flow_app, sid, "offer tally")
    row = _pending(flow_app, "offer_plugin")
    assert row["tool_call"]["input"]["where"] == "this conversation"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
    added = wait_until(lambda: done.get("offer tally"), timeout=60)
    assert added["plugin"] == "tally" and added["trusted_now"] is True, added
    assert "private-local/tally" in flow_app.get(f"/v1/sessions/{sid}/agent").json()["delta"]["add"]


def test_under_bypass_the_conversation_that_wrote_it_is_not_asked(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "bypass")
    _ask(flow_app, sid, done, "make tally")
    started = _ask(flow_app, sid, done, "spawn with private-local/tally")
    assert started.get("handle"), started
    market = flow_app.app.state.plugin_service.store.marketplaces()["private-local"]
    assert market["trust_reason"] == "authored_bypass"
