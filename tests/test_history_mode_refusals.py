"""History mode refuses what needs clio-core, typed -- and undo still rolls the context back.

Manual compaction and the context routes answer ``history_mode_unsupported`` (409), never
a silent no-op or an "empty" skip. Undo/rewind drops the session's History scopes so the
next turn reseeds them from the rolled-back transcript: the model never sees an undone turn.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.arc import history_mode
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.app import build_app
from clio_agent.gact.compaction import maybe_autocompact
from clio_agent.gact.session_store import _append_session_message
from clio_agent.gact.types import Message, Part, Tokens
from tests._scripted_engine import Reply, scripted_lm

pytestmark = pytest.mark.history_mode

SCOPE = "main"


@pytest.fixture(autouse=True)
def no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)
    history_mode.resolve()


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


def _run_turn(app: Any, sid: str, question: str) -> None:
    tokens = [
        ctx.set_app(app),
        ctx.set_session_id(sid),
        ctx.set_react_scope(SCOPE),
        ctx.set_react_session(sid),
    ]
    lm, _ = scripted_lm([Reply(text="ok")])
    try:
        with dspy.context(lm=lm):
            ClioReAct("question -> answer", tools=[])(question=question)
            maybe_autocompact()  # History mode has no compaction: never touches the plane
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _error(response: Any) -> str:
    body = response.json()
    return (body.get("detail") or body)["error"]["error"]


def test_manual_compaction_is_refused_typed(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _append_session_message(app, sid, _text("m1", sid, "hello", "user"))

        response = client.post(f"/v1/sessions/{sid}/compact", json={})

    assert response.status_code == 409, response.json()
    assert _error(response) == "history_mode_unsupported"


def test_context_search_is_refused_typed(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        response = client.get(f"/v1/sessions/{sid}/context/search", params={"q": "x"})

    assert response.status_code == 409, response.json()
    assert _error(response) == "history_mode_unsupported"


def test_undo_drops_the_sessions_history_scopes(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _append_session_message(app, sid, _text("m1", sid, "first question", "user"))
        _run_turn(app, sid, "first question")
        _append_session_message(app, sid, _text("m2", sid, "ok", "assistant"))
        plane = app.state.history_plane
        before = len(plane.history(sid, SCOPE).messages)

        response = client.post(f"/v1/sessions/{sid}/undo", json={"count": 2})

    assert response.status_code == 200, response.json()
    assert before > 0
    assert plane.history(sid, SCOPE).messages == []
