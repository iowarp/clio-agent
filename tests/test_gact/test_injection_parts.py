"""Harness data the agent was given is shown to the user as an ``injection`` part.

The agent and the user see the same thing: every CLIO addition in the agent's
context (a ``[clio: <source>]`` message) and every note the harness adds to a tool
call's result has an ``injection`` part in the transcript, with the exact text the
agent got (the UI-vs-agent projection contract of the agent-loop rebuild).
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import clio_react, clio_react_record
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.app import build_app
from clio_agent.gact.injection_parts import emit_injection
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.tools import injections
from tests._scripted_engine import Reply, calls, scripted_lm

SESSION, SCOPE = "s1", "agentA"


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _capture(source: str, text: str, *, call_id: str = "", agent_id: str = "") -> bool:
        rows.append({"source": source, "text": text, "call_id": call_id})
        return True

    monkeypatch.setattr(clio_react_record, "emit_injection", _capture)
    monkeypatch.setattr(clio_react, "emit_injection", _capture)
    return rows


@contextmanager
def _plane(arc: ARCMemory, **state: Any) -> Iterator[None]:
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc, **state))
    tokens = [ctx.set_app(app), ctx.set_react_scope(SCOPE), ctx.set_react_session(SESSION)]
    try:
        yield
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _clio_messages(request: Any) -> list[str]:
    texts = []
    for message in request.messages:
        for part in message.parts:
            text = getattr(part, "text", "")
            if message.role == "user" and text.startswith("[clio: "):
                texts.append(text)
    return texts


def test_every_clio_addition_the_agent_sees_is_shown_to_the_user(
    tmp_path: Path, emitted: list[dict[str, Any]]
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arrivals = [[], [("task_results", "child: done")]]

    def drain() -> list[tuple[str, str]]:
        return arrivals.pop(0) if arrivals else []

    lm, engine = scripted_lm([calls(("search", {"q": "x"})), Reply(text="ok")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "hits", name="search")])
    with _plane(arc, pending_loop_inbox_drain=drain), dspy.context(lm=lm):
        token = ctx.set_turn_injections([("todos", "- [ ] load"), ("plan_mode", "plan first")])
        try:
            agent(question="q")
        finally:
            ctx.reset(token)

    seen_by_agent = _clio_messages(engine.requests[-1])
    shown = [f"[clio: {row['source']}]\n{row['text']}" for row in emitted]
    assert seen_by_agent == shown
    assert [row["source"] for row in emitted] == ["tool_use", "todos", "plan_mode", "task_results"]


def test_a_note_on_a_tool_call_is_shown_with_its_call(
    tmp_path: Path, emitted: list[dict[str, Any]]
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))

    def search(q: str) -> str:
        hint = (
            "[clio: path_hint]\nargument 'path': 'a.csv' does not exist. Did you mean 'data/a.csv'?"
        )
        injections.note("path_hint", hint)
        return f"{hint}\n\nnot found"

    lm, engine = scripted_lm([calls(("search", {"q": "x"})), Reply(text="ok")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(search, name="search")])
    with _plane(arc), dspy.context(lm=lm):
        agent(question="q")

    [row] = [r for r in emitted if r["call_id"]]  # the note on the call, not a turn addition
    assert (row["source"], row["call_id"]) == ("path_hint", "call_0_0")
    observation = engine.requests[1].messages[-1].parts[0].content[0].text
    assert row["text"] in observation, "the user sees exactly what the agent was told"


def test_an_injection_lands_in_the_transcript_and_on_the_wire(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        tokens = [ctx.set_app(app), ctx.set_session_id(sid)]
        try:
            assert emit_injection("todos", "- [ ] load", call_id="c1")
        finally:
            for token in reversed(tokens):
                ctx.reset(token)
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        [part] = [p for m in messages for p in m["parts"] if p["type"] == "injection"]
        assert (part["source"], part["text"]) == ("todos", "- [ ] load")
        assert part["metadata"] == {"actor": "algorithm", "call_id": "c1"}
        block = part_to_v3_block(part)
        assert (block["type"], block["source"], block["call_id"]) == ("injection", "todos", "c1")


def test_no_app_session_means_nothing_to_show() -> None:
    assert emit_injection("todos", "x") is False


def test_the_executor_notes_what_it_tells_the_agent(tmp_path: Path) -> None:
    """The real fixes (a path hint here) feed the per-call collector."""
    from clio_agent.tools.execution import SyncMCPToolExecutor
    from tests.test_tools.test_execution import NotFoundClient, _reference

    _reference(tmp_path)
    executor = SyncMCPToolExecutor(object(), timeout=1.0, client_factory=lambda _: NotFoundClient())
    try:
        with injections.collect() as told:
            result = executor.call_tool(
                "fake_echo", {"filepath": str(tmp_path / "x" / "reference.fasta")}
            )
    finally:
        executor.close()
    [(source, text)] = told
    assert source == "path_hint" and result.startswith(text)
