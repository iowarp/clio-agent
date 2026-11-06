"""The person summons a subagent or a server with a ``summon`` part."""

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


def test_two_summoned_subagents_start_as_the_persons_children(market: Path) -> None:
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
        children = _wait(lambda: len(_children(c, sid)) == 2 and _children(c, sid), "children")
        message_id = resp.json()["message_id"]
        for child in children:
            assert child.metadata["started_by"] == {"kind": "person", "message_id": message_id}
        user = next(
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "user"
        )
        summons = user["metadata"]["summons"]
        assert [row["role"] for row in summons["subagents"]] == ["explore", "review"]
        assert "agent_override" not in user["metadata"]
        handles = [row["handle"] for row in summons["subagents"]]
        parent = _wait(
            lambda: next((q for q in agent.questions if "The person started" in q), None),
            "the parent's turn",
        )
        for role, handle in zip(("explore", "review"), handles, strict=True):
            assert f"The person started {role} as task {handle} on this message." in parent
        for child in children:
            # The task is the text without the summon tokens.
            assert _wait(lambda child=child: _task_of(c, child.id), "the child's task") == (
                "where is the spill folder written?"
            )
        # Each child's report reaches the agent that waits on it, the parent.
        _wait(lambda: _reports(c, sid) == 2, "the children's reports")


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


def test_a_command_files_agent_summons_that_subagent_on_the_recipe(market: Path) -> None:
    agent = _Recorder()
    with TestClient(build_app(sessions_path=market / "s.json", agent=agent)) as c:
        sid = _session(c)
        resp = c.post(f"/v1/sessions/{sid}/commands/ask-review", json={"input": "the diff"})
        assert resp.status_code == 200, resp.text
        (child,) = _wait(lambda: _children(c, sid), "the review child")
        assert child.metadata["started_by"]["kind"] == "person"
        assert _wait(lambda: _task_of(c, child.id), "the child's task") == (
            "Review the diff carefully."
        )
        parent = _wait(
            lambda: next((q for q in agent.questions if q.startswith("/ask-review")), None),
            "the parent's turn",
        )
        assert parent.startswith("/ask-review the diff\n\nThe person started review as task task_")
        user = next(
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "user"
        )
        assert "agent_override" not in user["metadata"]


def _catalog() -> ToolCatalog:
    schema = {"type": "object", "properties": {"term": {"type": "string"}}}
    return ToolCatalog(
        [
            CatalogEntry("fixture_docs_lookup_term", "fixture_docs", "Look a term up.", schema),
            CatalogEntry("fixture_docs_count_words", "fixture_docs", "Count words.", schema),
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
    entries = [("memory", "M"), (SERVER_SUMMON_SOURCE, '{"server": "fixture_docs"}')]
    (memory, (source, text)) = expand_server_summons(entries, program)
    assert memory == ("memory", "M") and source == SERVER_SUMMON_SOURCE
    assert text.startswith(
        "The person pointed this message at the fixture_docs tools.\n\nLoaded 2 tools"
    )
    assert "## fixture_docs_lookup_term" in text and "hdf5_list" not in text
    assert loaded == [["fixture_docs_count_words", "fixture_docs_lookup_term"]]


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


def test_a_summoned_message_projects_its_chips_and_children() -> None:
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
                "subagents": [{"role": "explore", "handle": "task_abc"}],
                "servers": [],
                "failed": [],
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
    assert row["metadata"]["summons"] == {"subagents": ["explore"], "servers": [], "failed": []}
    assert row["metadata"]["loaded_skill"] == "run-a-study"
    assert "task_abc" not in str(row)
