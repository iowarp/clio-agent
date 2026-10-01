"""The loud History mode runs real turns: the basics, on the real app and the real loop.

The platform has no clio-core binding (forced here), so the app has no ARC. The agent's
context is its scope's ``dspy.History`` on the app's History plane; the highway is fed
directly with every event marked ``context_mode: history``; after a restart the agent's
earlier turns come back from the transcript file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dspy
import pytest

from clio_agent.arc import history_mode
from clio_agent.arc.history_plane import HistoryPlane
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import read_steps
from clio_agent.gact.app import build_app
from clio_agent.gact.session_store import _append_session_message
from clio_agent.gact.types import Message, Part, Tokens
from tests._scripted_engine import Reply, calls, scripted_lm, wire

pytestmark = pytest.mark.history_mode

SCOPE = "main"


@pytest.fixture(autouse=True)
def no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)
    history_mode.resolve()


@contextmanager
def _turn(app: Any, sid: str) -> Iterator[None]:
    tokens = [
        ctx.set_app(app),
        ctx.set_session_id(sid),
        ctx.set_react_scope(SCOPE),
        ctx.set_react_session(sid),
    ]
    try:
        yield
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _text(message_id: str, sid: str, text: str, role: str) -> Message:
    now = datetime.now(UTC).isoformat()
    return Message(
        id=message_id,
        session_id=sid,
        role=role,
        created_at=now,
        updated_at=now,
        parts=[Part(id=f"part_{message_id}", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def _session(app: Any) -> str:
    return app.state.sessions.create(workspace_id="ws_default", title="history").id


def test_the_app_has_no_arc_and_one_history_plane(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")

    assert app.state.arc is None
    with _turn(app, _session(app)):
        lm, _ = scripted_lm([Reply(text="hi")])
        with dspy.context(lm=lm):
            ClioReAct("question -> answer", tools=[])(question="hello")
    assert isinstance(app.state.history_plane, HistoryPlane)


def test_a_tool_turn_runs_on_the_scopes_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit
    monkeypatch.setattr(
        app.state.semantic_event_sink, "emit", lambda e: (events.append(e), real_emit(e))[1]
    )

    lm, engine = scripted_lm([calls(("search", {"q": "x"}), text="look"), Reply(text="SIO5")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "HITS", name="search")])
    with _turn(app, sid), dspy.context(lm=lm):
        pred = agent(question="which station?")

    assert (pred.answer, pred.termination_reason) == ("SIO5", "direct_response")
    assert wire(engine.requests[1])[-1] == ("tool", [("result", "call_0_0", "HITS", False)])
    history = app.state.history_plane.history(sid, SCOPE)
    assert isinstance(history, dspy.History)
    assert [r["kind"] for r in history.messages if r["kind"] in {"tool_call", "observation"}] == [
        "tool_call",
        "observation",
    ]
    assert pred.messages == read_steps(app.state.history_plane, sid, SCOPE)  # one record
    assert events, "the highway is fed in History mode"
    assert {e.payload.get("context_mode") for e in events} == {"history"}


def test_after_a_restart_the_earlier_turn_comes_back_from_the_transcript(
    tmp_path: Path,
) -> None:
    first = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(first)
    _append_session_message(first, sid, _text("m1", sid, "which station is it?", "user"))
    _append_session_message(first, sid, _text("m2", sid, "It is SIO5.", "assistant"))

    restarted = build_app(sessions_path=tmp_path / "s.json")  # a new process: empty plane
    _append_session_message(restarted, sid, _text("m3", sid, "and its elevation?", "user"))
    lm, engine = scripted_lm([Reply(text="12 m")])
    with _turn(restarted, sid), dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[])(question="and its elevation?")

    texts = [text for _role, parts in wire(engine.requests[0]) for _k, text in parts]
    assert "which station is it?" in texts
    assert "It is SIO5." in texts
    assert texts[-1] == "and its elevation?"
