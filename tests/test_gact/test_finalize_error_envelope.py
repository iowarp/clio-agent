"""#756: an exception in the turn finalize region must settle the turn.

Everything after ``_run_turn_in_background``'s forward except-chain (answer
grounding, part assembly, diff indexing, publishes, persistence) runs inside a
fire-and-forget task. Before the fix, an exception there died silently: no
``message.completed``, no ``session.status_changed``, session wedged in
``running`` forever. The finalize region is now wrapped in the turn's error
envelope, so an injected finalize exception must yield a visible error turn
and a terminal session status.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to the
# ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

from .test_post_messages import FakeClioAgent


def test_finalize_exception_settles_turn_with_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalize-only helper raising must not wedge the session in running."""

    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    # _enrich_cancellation_error_info runs unconditionally in the finalize
    # region (after the forward except-chain), so raising here simulates any
    # finalize crash: grounding, Part construction, Pydantic validation, ...
    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]

        # Poll until the session leaves 'running' (or time out — the pre-fix
        # symptom: the background task dies silently and the status never
        # changes, so this loop exhausts and the assert below reports it).
        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)

        history = app.state.bus._history.get(sid, [])
        completed = [ev for ev in history if ev.type == "message.completed"]

        assert status == "error", (
            f"finalize exception must settle the session to a terminal status; "
            f"session stayed {status!r} with "
            f"{len(completed)} message.completed event(s)"
        )

        # The failure is a visible error turn on the bus...
        assert completed, "finalize exception must still publish message.completed"
        payload = completed[-1].payload
        assert payload["turn_id"] == user_id
        assert payload["stop_reason"] == "error"
        assert payload["error_info"]["error"] == "finalize_error"
        assert "simulated finalize failure" in payload["error_info"]["message"]
        assert payload["error_info"]["details"]["reason"] == "turn_finalize_error"

        status_events = [ev for ev in history if ev.type == "session.status_changed"]
        assert status_events and status_events[-1].payload["status"] == "error"

        # ...and in the persisted transcript.
        msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        error_turns = [m for m in msgs if m["role"] == "assistant" and m.get("turn_id") == user_id]
        assert error_turns, "error turn must land in the persisted transcript"
        assert error_turns[0]["stop_reason"] == "error"


def test_finalize_survives_a_prologue_that_skipped_context_file_provenance(
    tmp_path: Path,
) -> None:
    """#1331 review round: a turn whose prologue never ran
    ``turn_start_offloop.prepare_turn_off_loop`` (both of its
    ``state.context_file_provenance = ...`` assignments) must still finalize
    cleanly, not crash with ``TypeError: 'NoneType' object is not subscriptable``
    at ``turn_finalize.py``'s ``state.context_file_provenance["files"]``.

    Drives ``finalize_turn`` directly on a ``TurnState`` built via
    ``new_turn_state`` (which does NOT touch ``context_file_provenance`` --
    only the prologue does) with the other ``init=False`` fields
    (``workflow_schema``/``transcript``/``turn_cancel_event``) wired the same
    way ``turn.py``'s own linear body wires them, BEFORE the try block that
    calls ``prepare_turn_off_loop`` -- reproducing exactly the real ordering
    gap: a turn that reaches finalize without its off-loop prologue ever
    running (e.g. a crash/cancellation between that setup and the prologue
    call) still carries an initialized identity but an unset
    ``context_file_provenance``.
    """

    from clio_agent.gact.agents.resolution import _active_workflow_state_schema
    from clio_agent.gact.tool_observer import _open_turn_transcript
    from clio_agent.gact.turn_finalize import finalize_turn
    from clio_agent.gact.turn_state import new_turn_state
    from clio_agent.gact.turn_watchdog import make_turn_cancel_event
    from clio_agent.gact.types import Message, Part

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    now = "2026-09-12T00:00:00+00:00"
    user_msg = Message(
        id="msg_user_1",
        session_id=sid,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_user_1", type="text", text="hello")],
    )
    sess = app.state.sessions.get(sid)
    state = new_turn_state(app, sid, "hello", user_msg, "main", sess=sess, bus=app.state.bus)
    # Wire the other init=False fields exactly as turn.py's linear body does,
    # BEFORE the try block that calls prepare_turn_off_loop -- so this state
    # is otherwise fully turn-shaped, only missing the ONE prologue-only
    # assignment under test.
    state.workflow_schema = _active_workflow_state_schema(app, sid)
    state.transcript = _open_turn_transcript(app, sid, state.turn_id)
    make_turn_cancel_event(state)
    # NEVER call prepare_turn_off_loop: context_file_provenance stays at
    # TurnState's own default. context_frame shares the same None-until-
    # prologue shape (also only set by prepare_turn_off_loop, via
    # _record_context_frame) and finalize_turn subscripts it too
    # (state.context_frame["id"]) -- out of THIS fix's scope, so stand in a
    # minimal real value here to isolate the one field under test.
    state.context_frame = {"id": "frame-test"}
    state.answer_text = "a real answer, so this isn't the empty_response branch"

    pred = object()  # every read of it uses getattr(..., default) or is guarded
    finalize_turn(
        state,
        pred,
        drain_observed_tool_calls=lambda calls: calls,
        update_retry_attempt=lambda *args, **kwargs: None,
    )  # must not raise

    assert state.context_file_provenance == {
        "status": "unset",
        "count": 0,
        "max_inline_bytes": state.context_file_provenance["max_inline_bytes"],
        "files": [],
    }
    assert "context_files" not in state.assistant_metadata
