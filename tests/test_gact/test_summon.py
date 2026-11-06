"""The person summons a subagent, workflow, flow or server with a ``summon`` part."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.summon import SERVER_SUMMON_SOURCE, expand_server_summons
from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog
from tests._plugin_fixtures import custom_marketplace

pytestmark = pytest.mark.usefixtures("host_agent_executor")

AGENT = """
from clio_agent.sdk import Agent


class Reviewing(Agent):
    id = "reviewing"
    version = "1.0.0"
    role = "a data review agent"
    plugins = Agent.plugins + ["workspace", "review"]
"""

SUBAGENTS = """
from clio_agent.sdk import Subagent

explore = Subagent("explore", prompt="You find things.", description="Finds where things are.")
review = Subagent("review", prompt="You review.", description="Reviews a change.")
"""

COMMAND = "---\nname: ask-review\nagent: review\n---\nReview $ARGUMENTS carefully."

WORKFLOW = """{
  "description": "Look, then review.",
  "inputs": [{"name": "target", "description": "What to review"}],
  "steps": [
    {"id": "look", "target": "explore", "input": "Look at {target}."},
    {"id": "check", "target": "review", "after": ["look"], "input": "Review {target}: $look"}
  ]
}"""


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Recorder:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.lock = threading.Lock()

    def forward(self, question: str = "", *args: Any, **kwargs: Any) -> _Pred:
        with self.lock:
            self.questions.append(question)
        return _Pred(answer="DONE")


@pytest.fixture()
def market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "review": {
                "files": {
                    "ai.iowarp.clio/subagents/review.py": SUBAGENTS,
                    "ai.iowarp.clio/commands/ask-review.md": COMMAND,
                    "ai.iowarp.clio/workflows/review-pass.json": WORKFLOW,
                }
            }
        },
        agents={"reviewing.py": AGENT},
    )
    return tmp_path


def _session(client: TestClient) -> str:
    response = client.post(
        "/v1/sessions", json={"title": "t", "metadata": {"agent_ref": "clio/reviewing"}}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _summon(target: str, kind: str = "subagent") -> dict[str, str]:
    return {"type": "summon", "target_kind": kind, "target": target}


def _wait(predicate: Any, what: str) -> Any:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _children(client: TestClient, sid: str) -> list[dict[str, Any]]:
    return [
        row
        for row in client.app.state.sessions.list()
        if row.parent_session_id == sid and row.metadata.get("started_by")
    ]


def _task_of(client: TestClient, child_id: str) -> str:
    """The child's task, as its first turn received it (the injection after its preamble)."""
    for message in client.app.state.messages.get(child_id, []):
        injected = [p.text for p in message.parts if p.type == "injection"]
        if injected:
            return injected[-1]
    return ""


def _reports(client: TestClient, sid: str) -> int:
    return sum(
        1
        for message in client.app.state.messages.get(sid, [])
        for part in message.parts
        if part.type == "injection" and "task_result" in (part.text or "")
    )


def test_an_unknown_summons_is_refused_and_nothing_starts(market: Path) -> None:
    with TestClient(build_app(sessions_path=market / "s.json", agent=_Recorder())) as c:
        sid = _session(c)
        resp = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "look"}, _summon("nobody")]},
        )
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["error"] == "summon_unknown_target"
        assert error["details"]["unknown"] == [{"target_kind": "subagent", "target": "nobody"}]
        assert error["details"]["subagents"] == ["explore", "review"]
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []


def _user(client: TestClient, sid: str) -> dict[str, Any]:
    return next(
        m
        for m in client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        if m["role"] == "user"
    )


def _answer(client: TestClient, sid: str) -> dict[str, Any]:
    return _wait(
        lambda: next(
            (
                m
                for m in client.app.state.messages.get(sid, [])
                if m.role == "assistant" and m.metadata.get("summons_outcome")
            ),
            None,
        ),
        "the turn's answer",
    )


def test_summoned_subagents_start_nothing_and_are_told_to_the_agent(market: Path) -> None:
    """No pre-start: the turn's agent reads who was asked for and writes the brief."""

    from clio_agent.gact.summon import summon_injections

    agent = _Recorder()
    with TestClient(build_app(sessions_path=market / "s.json", agent=agent)) as c:
        sid = _session(c)
        resp = c.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "parts": [
                    {"type": "text", "text": "@explore @review where is the spill folder written?"},
                    _summon("explore"),
                    _summon("review"),
                ]
            },
        )
        assert resp.status_code == 202, resp.text
        user = _user(c, sid)
        summons = user["metadata"]["summons"]
        assert summons["subagents"] == [{"role": "explore"}, {"role": "review"}]
        assert [p["target"] for p in user["parts"] if p["type"] == "summon"] == [
            "explore",
            "review",
        ]
        stored = c.app.state.messages[sid][0].metadata["summons"]
        additions = summon_injections(c.app, sid, stored)
        assert [source for source, _ in additions] == ["summon_subagent", "summon_subagent"]
        text = additions[0][1]
        assert text.startswith(
            "The person asked for explore to work on this message (Finds where things are.)."
        )
        assert 'call spawn_agent_task with agent="explore"' in text
        assert 'agent="review"' in additions[1][1]
        # The stub agent called nothing: no child, and the answer says what was not started.
        outcome = _answer(c, sid).metadata["summons_outcome"]
        assert outcome == {
            "started": [],
            "not_started": [
                {"kind": "subagent", "target": "explore", "reason": "not_called"},
                {"kind": "subagent", "target": "review", "reason": "not_called"},
            ],
        }
        assert _children(c, sid) == []


def test_a_call_naming_a_summoned_target_is_the_persons(market: Path) -> None:
    from clio_agent.gact import context as _ctx
    from clio_agent.gact import summon

    app = SimpleNamespace(state=SimpleNamespace())
    summon.open_turn(
        app,
        "turn_1",
        "msg_1",
        {"subagents": [{"role": "explore"}], "workflows": ["draft-review"], "flows": []},
    )
    app_token = _ctx.set_app(app)
    turn_token = _ctx.set_turn_id_token("turn_1")
    try:
        claimed = summon.claim("subagent", "explore")
        assert summon.child_metadata(claimed) == {
            "started_by": {"kind": "person", "message_id": "msg_1"}
        }
        assert summon.claim("subagent", "explore") is None, "claimed once"
        assert summon.claim("subagent", "review") is None, "not summoned"
        summon.started(claimed, {"handle": "task_1"})
    finally:
        _ctx.reset(turn_token)
        _ctx.reset(app_token)
    assert summon.settle_turn(app, "turn_1") == {
        "started": [{"kind": "subagent", "target": "explore", "handle": "task_1"}],
        "not_started": [{"kind": "workflow", "target": "draft-review", "reason": "not_called"}],
    }
    assert summon.settle_turn(app, "turn_1") is None


def test_a_deny_rule_on_spawning_refuses_the_summons(market: Path) -> None:
    with TestClient(build_app(sessions_path=market / "s.json", agent=_Recorder())) as c:
        sid = _session(c)
        c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "spawn_agent_task",
                        "action": "deny",
                    }
                ]
            },
        )
        resp = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "find it"}, _summon("explore")]},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["error"] == "summon_denied"
        assert c.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []


def test_a_command_files_agent_summons_that_subagent_with_the_recipe(market: Path) -> None:
    agent = _Recorder()
    with TestClient(build_app(sessions_path=market / "s.json", agent=agent)) as c:
        sid = _session(c)
        resp = c.post(f"/v1/sessions/{sid}/commands/ask-review", json={"input": "the diff"})
        assert resp.status_code == 200, resp.text
        # The agent reads the recipe and writes the reviewer's brief from it.
        _wait(lambda: "Review the diff carefully." in agent.questions, "the parent's turn")
        user = _user(c, sid)
        assert user["metadata"]["summons"]["subagents"] == [{"role": "review"}]
        assert "agent_override" not in user["metadata"]
        assert _children(c, sid) == []


def test_a_declared_workflow_is_summonable_and_runs_by_id(market: Path) -> None:
    from clio_agent.gact.agents.resolution import session_resolution
    from clio_agent.gact.summon import summon_injections
    from clio_agent.plugins.workflows import bind_inputs, find_workflow

    with TestClient(build_app(sessions_path=market / "s.json", agent=_Recorder())) as c:
        sid = _session(c)
        body = c.get(f"/v1/sessions/{sid}/summon-targets").json()
        (row,) = body["workflows"]
        assert row["id"] == "review-pass" and row["plugin"] == "review"
        assert row["inputs"] == [
            {"name": "target", "description": "What to review", "required": True}
        ]
        assert all(flow["id"] not in {"react", "workflow"} for flow in body["flows"])
        resp = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "go"}, _summon("review-pass", "workflow")]},
        )
        assert resp.status_code == 202, resp.text
        additions = dict(
            summon_injections(c.app, sid, c.app.state.messages[sid][0].metadata["summons"])
        )
        assert (
            'Call run_workflow with workflow="review-pass" and inputs for target'
            in (additions["summon_workflow"])
        )
        _plugin, record = find_workflow(session_resolution(c.app, sid), "review/review-pass")
        steps = bind_inputs(record, {"target": "the diff"})
        assert steps[0]["input"] == "Look at the diff." and steps[1]["after"] == ["look"]
        with pytest.raises(ValueError, match="needs input 'target'"):
            bind_inputs(record, {})
        with pytest.raises(ValueError, match="has no input 'other'"):
            bind_inputs(record, {"target": "x", "other": "y"})


def _catalog() -> ToolCatalog:
    schema = {"type": "object", "properties": {"term": {"type": "string"}}}
    return ToolCatalog(
        [
            CatalogEntry("fixture-docs_lookup_term", "fixture-docs", "Look a term up.", schema),
            CatalogEntry("fixture-docs_count_words", "fixture-docs", "Count words.", schema),
            CatalogEntry("hdf5_list", "hdf5", "List datasets.", schema),
        ]
    )


def test_a_server_summons_loads_that_servers_deferred_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import search_tools

    loaded: list[list[str]] = []
    monkeypatch.setattr(
        search_tools, "_record_loaded", lambda app, sid, names: loaded.append(names)
    )
    program = SimpleNamespace(_clio_deferred_catalog=_catalog())
    entries = [("memory", "M"), (SERVER_SUMMON_SOURCE, '{"server": "fixture-docs"}')]
    (memory, (source, text)) = expand_server_summons(entries, program)
    assert memory == ("memory", "M") and source == SERVER_SUMMON_SOURCE
    assert text.startswith(
        "The person pointed this message at the fixture-docs tools.\n\nLoaded 2 tools"
    )
    assert "## fixture-docs_lookup_term" in text and "hdf5_list" not in text
    assert loaded == [["fixture-docs_count_words", "fixture-docs_lookup_term"]]


def test_a_server_whose_tools_are_listed_gets_the_note_only() -> None:
    program = SimpleNamespace(_clio_deferred_catalog=_catalog())
    ((_, text),) = expand_server_summons(
        [(SERVER_SUMMON_SOURCE, '{"server": "workspace"}')], program
    )
    assert text == "The person pointed this message at the workspace tools."


def test_the_summon_targets_are_the_agents_subagents_and_servers(market: Path) -> None:
    with TestClient(build_app(sessions_path=market / "s.json", agent=_Recorder())) as c:
        sid = _session(c)
        body = c.get(f"/v1/sessions/{sid}/summon-targets").json()
        assert [row["id"] for row in body["subagents"]] == ["explore", "review"]
        assert body["subagents"][0]["description"] == "Finds where things are."
        assert all(
            {"name", "description", "deferred", "tools"} <= set(row) for row in body["servers"]
        )
        assert c.get("/v1/sessions/sess_nope/summon-targets").status_code == 404


def test_a_summoned_message_projects_its_chips_and_the_answer_its_outcome() -> None:
    from clio_agent.gact.protocol.v3.message import message_to_v3

    wire = {
        "id": "user_1",
        "session_id": "sess_1",
        "role": "user",
        "created_at": "2026-11-06T00:00:00Z",
        "parts": [
            {"id": "p1", "type": "text", "text": "@explore where is it?"},
            {"id": "p2", "type": "summon", "target_kind": "subagent", "target": "explore"},
        ],
        "metadata": {
            "summons": {
                "subagents": [{"role": "explore"}],
                "servers": [],
                "workflows": ["draft-review"],
                "flows": [],
            },
            "loaded_skill": "run-a-study",
        },
    }
    row = message_to_v3(wire)
    assert row["blocks"][1] == {
        "id": "p2",
        "type": "summon",
        "target_kind": "subagent",
        "target": "explore",
    }
    assert row["metadata"]["summons"] == {
        "subagents": ["explore"],
        "servers": [],
        "workflows": ["draft-review"],
        "flows": [],
        "failed": [],
    }
    assert row["metadata"]["loaded_skill"] == "run-a-study"
    answer = message_to_v3(
        {
            **wire,
            "id": "asst_1",
            "role": "assistant",
            "parts": [{"id": "p3", "type": "text", "text": "done"}],
            "metadata": {
                "summons_outcome": {
                    "started": [{"kind": "subagent", "target": "explore", "handle": "task_abc"}],
                    "not_started": [
                        {"kind": "workflow", "target": "draft-review", "reason": "not_called"}
                    ],
                }
            },
        }
    )
    assert answer["metadata"]["summons_outcome"] == {
        "started": [{"kind": "subagent", "target": "explore"}],
        "not_started": [{"kind": "workflow", "target": "draft-review", "reason": "not_called"}],
    }
    assert "task_abc" not in str(answer)


def test_a_handle_list_written_as_one_json_string_is_read_as_the_list() -> None:
    """Live N12: a model passed wait_tasks its two handles as one JSON string."""

    from clio_agent.gact.task_operations import _handles

    assert _handles('["task_a", "task_b"]') == ("task_a", "task_b")
    assert _handles("task_a") == ("task_a",)
    assert _handles(["task_a", "task_a"]) == ("task_a",)
