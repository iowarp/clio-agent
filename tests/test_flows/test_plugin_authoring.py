"""make_plugin: a plugin Clio writes is asked about in the conversation, or adopted under bypass."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.test_flows.conftest import default_program, post, wait_until

FILES = {
    "plugin.json": json.dumps(
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "tally",
            "version": "0.1.0",
            "description": "Counts votes.",
            "author": {"name": "Clio"},
            "license": "BSD-3-Clause",
            "extensions": {"ai.iowarp.clio": {"requires": ["delegation"]}},
        }
    ),
    "ai.iowarp.clio/flows/tally.py": (
        "from clio_agent.sdk import Flow\n\n\n"
        "class Tally(Flow):\n"
        '    id = "tally"\n'
        '    description = "Counts votes."\n'
        "    shape = 'railroad-beta\\ntally = terminal(\"count\");'\n\n"
        "    async def forward(self, task, ctx):\n"
        '        return {"answer": "counted"}\n'
    ),
}


def _maker(client: TestClient) -> dict[str, Any]:
    """Script the conversation's model to call make_plugin once."""

    from clio_agent.gact.plugin_authoring import make_plugin

    made: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        import dspy

        if question.startswith("make the plugin") and not made:
            made.update(asyncio.run(make_plugin("tally", FILES)))
            return dspy.Prediction(answer="made")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return made


def _session(client: TestClient, approval_mode: str) -> str:
    response = client.post(
        "/v1/sessions",
        json={
            "title": "author",
            "metadata": {"agent_ref": "clio/orchestrator"},
            "approval_mode": approval_mode,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _store(client: TestClient) -> Any:
    return client.app.state.plugin_service.store


def _delta(client: TestClient, sid: str) -> list[str]:
    return client.get(f"/v1/sessions/{sid}/agent").json()["delta"]["add"]


def _question(client: TestClient) -> dict[str, Any]:
    def pending() -> dict[str, Any] | None:
        rows = client.get("/v1/questions", params={"status": "pending"}).json()["questions"]
        return next((row for row in rows if row.get("source") == "flow"), None)

    return wait_until(pending, timeout=60)


def test_under_bypass_a_written_plugin_is_trusted_and_added_at_once(
    flow_app: TestClient,
) -> None:
    made = _maker(flow_app)
    sid = _session(flow_app, "bypass")
    post(flow_app, sid, "make the plugin")
    wait_until(lambda: made, timeout=60)
    assert made["installed"] and made["trusted"], made
    assert made["added"] == ["tally/tally"]
    entry = _store(flow_app).marketplaces()["tally"]
    assert entry["trusted"] and entry["trust_reason"] == "authored_bypass"
    assert "tally/tally" in _delta(flow_app, sid)


def test_otherwise_the_person_is_asked_with_the_review_and_yes_adopts_it(
    flow_app: TestClient,
) -> None:
    made = _maker(flow_app)
    sid = _session(flow_app, "ask")
    post(flow_app, sid, "make the plugin")
    wait_until(lambda: made, timeout=60)
    assert made["installed"] and made["trusted"] is False, made
    assert made["request"]["accepted"], made
    assert not _store(flow_app).marketplaces()["tally"]["trusted"]

    question = _question(flow_app)
    assert question["prompt"] == "Use the tally plugin Clio wrote (tally)?"
    review = question["metadata"]["details"]["plugin_review"]
    assert review["flows"][0]["shape"] == 'railroad-beta\ntally = terminal("count");'
    assert review["reach"] == [] and "plugin.json" in review["files"]

    answer = flow_app.post(
        f"/v1/sessions/{question['session_id']}/questions/{question['id']}/answer",
        json={"value": "yes"},
    )
    assert answer.status_code == 200, answer.text
    wait_until(lambda: "tally/tally" in _delta(flow_app, sid), timeout=60)
    entry = _store(flow_app).marketplaces()["tally"]
    assert entry["trusted"] and entry["trust_reason"] == "approved_in_conversation"


def test_a_plugin_changed_after_writing_is_not_trusted_on_yes(flow_app: TestClient) -> None:
    from clio_agent.plugins.authored import authored_root

    made = _maker(flow_app)
    sid = _session(flow_app, "ask")
    post(flow_app, sid, "make the plugin")
    wait_until(lambda: made, timeout=60)
    question = _question(flow_app)
    folder = authored_root(_store(flow_app).root) / "tally"
    flow = Path(folder, "ai.iowarp.clio/flows/tally.py")
    flow.write_text(flow.read_text() + "\nimport subprocess\n")
    flow_app.post(
        f"/v1/sessions/{question['session_id']}/questions/{question['id']}/answer",
        json={"value": "yes"},
    )
    history = flow_app.app.state.task_history
    handle = made["request"]["handle"]
    wait_until(lambda: history.task_snapshot(handle).terminal_at is not None, timeout=60)
    result = json.dumps(history.read_content(history.task_snapshot(handle).result))
    assert "tally was not trusted" in result and "changed since" in result
    assert not _store(flow_app).marketplaces()["tally"]["trusted"]
    assert "tally/tally" not in _delta(flow_app, sid)
