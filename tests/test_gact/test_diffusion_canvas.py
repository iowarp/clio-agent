"""Diffusion (snapshot) streaming: the one canvas owner and its replace lane.

A diffusion model asked to show its denoising streams every step as the WHOLE
current answer. These tests pin the contract end to end on the stream side:

* :class:`SnapshotStream` shows each step through the replace lane and never
  concatenates snapshots; the final canvas becomes the call's only answer text;
* a stream that does not have the snapshot shape is reported with the typed
  ``diffusion_stream_shape_mismatch`` reason, never silently reinterpreted;
* the lane (:func:`note_lm_answer_canvas` -> :func:`emit_canvas`) publishes a
  TRANSIENT ``message.canvas`` frame that never enters the replay history, and
  the turn's transcript holds only the final text;
* the typed client parses the frame.
"""

from __future__ import annotations

import asyncio
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from dspy.lm15 import Message, Response, TextPart, ToolCallPart, Usage
from fastapi.testclient import TestClient

from clio_agent.client.events import MessageCanvas, parse_event
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import clio_react_canvas as canvas_mod
from clio_agent.gact.agents import clio_react_stream
from clio_agent.gact.agents.clio_react_canvas import (
    SHAPE_MISMATCH_REASON,
    SnapshotStream,
    step_is_snapshot,
    with_answer_text,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.tool_observer import _open_turn_transcript
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.turn_stream import begin_model_call, emit_canvas, emit_chunk
from clio_agent.runtime import lm_activity

DRAFT = "The capital of France is P#### and it is known for the E#### Tower."
FINAL = "The capital of France is Paris and it is known for the Eiffel Tower."


def _chunk(content: str | None = None, **delta: Any) -> dict[str, Any]:
    if content is not None:
        delta["content"] = content
    return {"choices": [{"index": 0, "delta": delta}]}


def _response(*parts: Any) -> Response:
    return Response(
        id="r1",
        model="mercury-3",
        message=Message.assistant(list(parts) or [TextPart(text="")]),
        finish_reason="stop",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


@pytest.fixture
def lanes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Capture the canvas lane, the append lane and the typed fallback note."""

    seen: dict[str, list[Any]] = {"canvas": [], "pieces": [], "notes": [], "audit": []}
    monkeypatch.setattr(
        lm_activity,
        "note_lm_answer_canvas",
        lambda text, step: seen["canvas"].append((step, text)) or True,
    )
    monkeypatch.setattr(
        clio_react_stream,
        "route_pieces",
        lambda pieces, unclosed=False: seen["pieces"].extend((p.kind, p.text) for p in pieces),
    )
    monkeypatch.setattr(
        "clio_agent.gact.stream_fallbacks.record_stream_fallback_note",
        lambda app, sid, reason, message: seen["notes"].append((sid, reason, message)),
    )
    monkeypatch.setattr(
        "clio_agent.runtime.stream_audit.stream_audit",
        lambda stage, **fields: seen["audit"].append({"stage": stage, **fields}),
    )
    return seen


# --------------------------------------------------------------------------- #
# The shape judgement
# --------------------------------------------------------------------------- #


def test_short_steps_are_not_judged() -> None:
    assert step_is_snapshot("He", "Hel") is None


def test_rewritten_versions_of_one_text_read_as_snapshots() -> None:
    assert step_is_snapshot(DRAFT, FINAL) is True
    assert step_is_snapshot(FINAL[:30], FINAL) is True
    assert step_is_snapshot(FINAL, FINAL) is True


def test_unrelated_appended_fragments_do_not_read_as_snapshots() -> None:
    assert (
        step_is_snapshot("The capital of France is Paris, ", "and it is known for its tower.")
        is False
    )


# --------------------------------------------------------------------------- #
# SnapshotStream: the one owner
# --------------------------------------------------------------------------- #


def test_snapshots_replace_and_the_final_canvas_is_the_only_answer(lanes) -> None:
    stream = SnapshotStream()
    for step in (DRAFT, DRAFT.replace("P####", "Paris"), FINAL):
        stream.feed(_chunk(step))

    # Every step reached the replace lane whole, numbered from 1.
    assert lanes["canvas"] == [
        (1, DRAFT),
        (2, DRAFT.replace("P####", "Paris")),
        (3, FINAL),
    ]
    # Nothing reached the append lane while streaming.
    assert lanes["pieces"] == []

    # DSPy concatenated the snapshots into nonsense; the owner replaces it.
    concatenated = DRAFT + DRAFT.replace("P####", "Paris") + FINAL
    settled = stream.finish(_response(TextPart(text=concatenated)), app=None, session_id="")

    assert [p.text for p in settled.message.parts if isinstance(p, TextPart)] == [FINAL]
    # The final text reaches the transcript once through the ordinary answer lane.
    assert lanes["pieces"] == [("answer", FINAL)]
    assert lanes["notes"] == []
    trace = next(r for r in lanes["audit"] if r["stage"] == "diffusion.stream")
    assert trace["steps"] == 3 and trace["shape"] == "snapshot"
    assert len(trace["step_ms"]) == 3


def test_the_same_chunk_object_is_shown_once(lanes) -> None:
    stream = SnapshotStream()
    chunk = _chunk(FINAL)
    stream.feed(chunk)
    stream.feed(chunk)
    assert lanes["canvas"] == [(1, FINAL)]


def test_reasoning_keeps_the_append_lane(lanes) -> None:
    stream = SnapshotStream()
    pieces = stream.feed(_chunk(None, reasoning_content="thinking it over"))
    assert [(p.kind, p.text) for p in pieces] == [("thinking", "thinking it over")]
    assert lanes["canvas"] == []


def test_appended_deltas_are_reported_typed_and_the_concatenation_is_kept(lanes) -> None:
    fragments = [
        "Paris is the capital and largest city of France. ",
        "It sits on the Seine river in the north of the country. ",
        "Its metropolitan area holds about twelve million people, ",
        "making it one of the largest urban areas in Europe.",
    ]
    stream = SnapshotStream()
    for fragment in fragments:
        stream.feed(_chunk(fragment))
    assert not stream.shaped_as_snapshots

    original = _response(TextPart(text="".join(fragments)))
    settled = stream.finish(original, app=object(), session_id="sid-1")

    # The response DSPy assembled is kept, not reinterpreted.
    assert settled is original
    assert lanes["pieces"] == [("answer", "".join(fragments))]
    [(sid, reason, message)] = lanes["notes"]
    assert sid == "sid-1"
    assert reason == SHAPE_MISMATCH_REASON
    assert "appended deltas" in message


def test_tool_calls_streamed_across_steps_are_reported(lanes) -> None:
    stream = SnapshotStream()
    call = {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}
    stream.feed(_chunk(None, tool_calls=[call]))
    stream.feed(_chunk(None, tool_calls=[call]))
    tool_part = ToolCallPart(id="c1", name="f", input={})
    original = _response(tool_part)

    settled = stream.finish(original, app=object(), session_id="sid-2")

    assert settled is original
    [(_sid, reason, message)] = lanes["notes"]
    assert reason == SHAPE_MISMATCH_REASON
    assert "tool calls arrived across 2 streamed steps" in message


def test_a_mismatch_off_turn_is_logged_not_dropped(lanes, caplog) -> None:
    stream = SnapshotStream()
    stream.tool_call_chunks = 2
    with caplog.at_level("WARNING", logger=canvas_mod.__name__):
        stream.finish(_response(), app=None, session_id="")
    assert lanes["notes"] == []
    assert SHAPE_MISMATCH_REASON in caplog.text


def test_with_answer_text_replaces_only_the_first_text_part() -> None:
    tool_part = ToolCallPart(id="c1", name="f", input={})
    response = _response(TextPart(text="a"), tool_part, TextPart(text="b"))
    settled = with_answer_text(response, FINAL)
    assert settled.message.parts == (TextPart(text=FINAL), tool_part)

    bare = with_answer_text(_response(tool_part), FINAL)
    assert bare.message.parts == (tool_part, TextPart(text=FINAL))


# --------------------------------------------------------------------------- #
# The replace lane: note_lm_answer_canvas -> emit_canvas -> message.canvas
# --------------------------------------------------------------------------- #


def _run_isolated(work) -> Any:
    """Run ``work`` on its own thread so the contextvars it binds never leak."""

    result: list[Any] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(work())
        except BaseException as exc:  # noqa: BLE001 - surface to the test thread
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(15)
    assert not thread.is_alive(), "lane thread hung"
    if errors:
        raise errors[0]
    return result[0] if result else None


def test_canvas_lane_is_a_no_op_off_turn() -> None:
    assert _run_isolated(lambda: lm_activity.note_lm_answer_canvas(FINAL, 1)) is False


def test_canvas_lane_is_closed_while_deltas_are_redirected() -> None:
    def work() -> bool:
        lm_activity.set_live_chunk_emitter(object(), lambda *a: None, None, None)
        lm_activity.set_live_canvas_emitter(lambda *a: None)
        return lm_activity.note_lm_answer_canvas(FINAL, 1)

    assert _run_isolated(work) is False


def _wait(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the live lane")  # DEBUG


class _StubAgent:
    def forward(self, **_: Any) -> Any:  # pragma: no cover - never driven here
        raise AssertionError("not driven")


def test_canvas_frames_are_transient_and_only_the_final_text_is_kept(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    sess = app.state.sessions.get(sid)
    transcript = _open_turn_transcript(app, sid, "turn_d1")
    published: list[Any] = []
    bus = app.state.bus
    real_publish = bus.publish

    def capture(event: Any) -> None:
        published.append(event)
        real_publish(event)

    bus.publish = capture  # type: ignore[method-assign]
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    def work() -> None:
        _ctx.set_turn_identity(app=app, session_id=sid, turn_id="turn_d1", trace_id="tr")
        _ctx.set_tool_session_id(sid)
        _ctx.set_react_scope("clio")
        _ctx.set_visible_answer_stream(True)
        state = TurnState(
            app=app,
            sid=sid,
            user_text="",
            user_msg=sess,
            turn_agent_id="clio",
            sess=sess,
            bus=bus,
            turn_id="turn_d1",
            trace_id="tr",
            retry_attempt_id="",
            native_images=[],
        )
        state.transcript = transcript
        state.active_agent_id = "clio"
        state.invocation_agent_id = "clio"
        lm_activity.set_live_chunk_emitter(
            loop,
            partial(emit_chunk, state),
            transcript.record_streamed_field_text,
            partial(begin_model_call, state),
        )
        lm_activity.set_live_canvas_emitter(partial(emit_canvas, state))
        assert lm_activity.note_lm_answer_canvas(DRAFT, 1) is True
        assert lm_activity.note_lm_answer_canvas(FINAL, 2) is True
        _wait(lambda: sum(e.type == "message.canvas" for e in published) == 2)
        lm_activity.note_lm_answer_delta(FINAL, field="next_thought")
        _wait(lambda: any(p.type == "text" for p in transcript.snapshot()))

    try:
        _run_isolated(work)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(5)
        loop.close()

    frames = [e for e in published if e.type == "message.canvas"]
    assert [(e.payload["step"], e.payload["text"]) for e in frames] == [(1, DRAFT), (2, FINAL)]
    assert all(e.transient for e in frames)
    assert frames[0].payload["message_id"] == transcript.message_id
    assert frames[0].payload["turn_id"] == "turn_d1"
    # The canvas never enters the replay history; the transcript holds the final text only.
    history = bus.session_events_since(sid)
    assert not any(e.type == "message.canvas" for e in history)
    texts = [p.text for p in transcript.finalize() if p.type == "text"]
    assert texts == [FINAL]


def test_client_parses_message_canvas() -> None:
    event = parse_event(
        0,
        {
            "type": "message.canvas",
            "session_id": "s1",
            "payload": {
                "turn_id": "t1",
                "message_id": "m1",
                "agent_id": "clio",
                "step": 3,
                "text": FINAL,
            },
        },
    )
    assert isinstance(event, MessageCanvas)
    assert (event.message_id, event.step, event.text) == ("m1", 3, FINAL)
