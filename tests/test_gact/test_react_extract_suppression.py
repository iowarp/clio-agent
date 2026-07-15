"""#878 — suppress a `kind: react` expert's EXTRACT (reasoning + answer) as a
visible transcript part, gated STRUCTURALLY on ``module.kind == react``.

A ``kind: react`` expert runs a DSPy ReAct loop (emitting a ``next_thought`` per
step) then a ``ChainOfThought`` EXTRACT emitting ``reasoning`` + ``answer``. Both
extract fields reach the live tap (``lm_activity.note_lm_answer_delta``) and, before
this fix, became visible text parts — a redundant ``reasoning``/``answer`` bullet
restating the final ``next_thought`` with no conversational standing.

THE LANDMINE this pins: ``reasoning`` is an OVERLOADED field name. A
``chain_of_thought`` expert (main/data/analysis/synthesis) emits ``reasoning`` as
its ENTIRE visible conversation. Suppressing by field name WITHOUT the kind gate
deletes those transcripts (the reverted first attempt). The gate must be strictly
``active_react_kind() == "react"``.

These drive the REAL tap -> ``emit_chunk`` -> ``append_text_delta`` path (the same
harness as ``test_next_thought_single_owner``) through a real app + ledger, then
reload via ``GET /v1/sessions/{sid}/messages`` to prove live == persisted-reload.
The ``stream_audit`` sink is monkeypatched so the no-silent-fallback reason is
asserted deterministically (no conf/file).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.tool_observer import _open_turn_transcript
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.turn_stream import emit_chunk
from clio_agent.gact.types import Message
from clio_agent.runtime.lm_activity import note_lm_answer_delta, set_live_chunk_emitter

REASONING = (
    "I resolved the place to a bounding box and set a conservative radius, then "
    "recorded the region as workflow_state.geospatial."
)
ANSWER = "Region resolved: Los Angeles County, center 34.05,-118.24, radius 100 km."
NEXT = "I call geo_geocode to look up Los Angeles."

_AUDIT_SINK_TARGETS = (
    "clio_agent.runtime.lm_activity.stream_audit",
    "clio_agent.gact.streaming.stream_audit",
)


def _install_audit_capture(monkeypatch) -> list[dict]:
    records: list[dict] = []

    def _capture(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    for target in _AUDIT_SINK_TARGETS:
        monkeypatch.setattr(target, _capture)
    return records


class _StubAgent:
    def forward(self, question: str, session_id: str):  # pragma: no cover - unused
        raise NotImplementedError


def _visible_parts(transcript, field: str) -> list:
    return [
        p
        for p in transcript.snapshot()
        if p.type == "text" and p.metadata.get("signature_field_name") == field
    ]


def _wait(pred, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)


def _run_executor_work(work) -> None:
    err: list[BaseException] = []

    def _target() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - surface to test thread
            err.append(exc)

    th = threading.Thread(target=_target)
    th.start()
    th.join(15)
    assert not th.is_alive(), "executor work thread hung"
    if err:
        raise err[0]


def _drive(
    tmp_path: Path,
    monkeypatch,
    *,
    scope: str,
    kind: str | None,
    deltas: list[tuple[str, str]] | None = None,
    action=None,
    visible_answer_stream: bool = True,
    wait_field: str | None = None,
) -> dict:
    """Drive ``note_lm_answer_delta`` for each (field, text) in ``deltas`` under a
    real react scope + module.kind, through the real emit path + ledger.

    ``action`` (if given) is run in the emitter-bound executor context INSTEAD of
    the deltas loop — used to drive a real ``_RetainingReAct.forward`` whose extract
    fires the tap. ``kind=None`` sets NO react_kind (unresolved-diagnostic)."""

    records = _install_audit_capture(monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    sess = app.state.sessions.get(sid)
    turn_id, trace_id = "turn_878", "trace_878"
    transcript = _open_turn_transcript(app, sid, turn_id)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    captured: dict[str, Any] = {}

    def work() -> None:
        _ctx.set_turn_identity(app=app, session_id=sid, turn_id=turn_id, trace_id=trace_id)
        _ctx.set_tool_session_id(sid)
        _ctx.set_react_scope(scope)
        if kind is not None:
            _ctx.set_react_kind(kind)
        _ctx.set_visible_answer_stream(visible_answer_stream)

        state = TurnState(
            app=app,
            sid=sid,
            user_text="",
            user_msg=sess,
            turn_agent_id=scope,
            sess=sess,
            bus=app.state.bus,
            turn_id=turn_id,
            trace_id=trace_id,
            retry_attempt_id="",
            native_images=[],
        )
        state.transcript = transcript
        state.active_agent_id = scope
        state.invocation_agent_id = scope

        set_live_chunk_emitter(
            loop,
            partial(emit_chunk, state),
            transcript.record_streamed_field_text,
        )
        if action is not None:
            captured["result"] = action()
        else:
            for field, text in deltas or []:
                note_lm_answer_delta(text, field=field)
        if wait_field is not None:
            _wait(lambda: bool(_visible_parts(transcript, wait_field)))
        else:
            # Give any scheduled cross-thread emit a beat to land (or not).
            time.sleep(0.2)

    try:
        _run_executor_work(work)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(5)
        loop.close()

    # Close the open streamed part so its cleaned buffer lands in ``part.text``
    # (finalize does this in production), then persist the settled snapshot to the
    # store so the GET /messages reload reads REAL persisted parts (not an empty
    # ledger that would make every "field absent" assertion trivially true).
    transcript.close_open_text()
    parts = transcript.snapshot()
    now = datetime.now(timezone.utc).isoformat()
    msg = Message(
        id="msg_878",
        session_id=sid,
        turn_id=turn_id,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=list(parts),
    )
    app.state.messages.setdefault(sid, []).append(msg)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.append(sid, msg)

    return {
        "app": app,
        "client": client,
        "sid": sid,
        "turn_id": turn_id,
        "transcript": transcript,
        "records": records,
        "result": captured.get("result"),
    }


def _reasons(records: list[dict]) -> list[str]:
    return [r.get("duplicate_reason") for r in records if r.get("stage") == "bridge.contract_field"]


def _persisted_text_fields(client: TestClient, sid: str) -> list[str]:
    resp = client.get(f"/v1/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    fields: list[str] = []
    for m in resp.json()["messages"]:
        for p in m.get("parts", []):
            if p.get("type") == "text":
                fields.append(p.get("metadata", {}).get("signature_field_name") or "")
    return fields


# --------------------------------------------------------------------------- #
# ANTI-REGRESSION (write first): chain_of_thought experts are UNTOUCHED.       #
# This would FAIL under a naive field-name-only suppression of `reasoning`.    #
# --------------------------------------------------------------------------- #


def test_chain_of_thought_reasoning_stays_visible(tmp_path, monkeypatch) -> None:
    """main/data/analysis: their ``reasoning`` IS their visible conversation and
    MUST remain a visible text part. A field-name-only suppression deletes it."""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="analysis",
        kind="chain_of_thought",
        deltas=[("reasoning", REASONING)],
        wait_field="reasoning",
    )
    visible = _visible_parts(facts["transcript"], "reasoning")
    assert len(visible) == 1, "CoT reasoning must stay a visible text part"
    assert visible[0].text.strip()
    # It was NOT suppressed as a react extract.
    assert "react_extract_field_suppressed" not in _reasons(facts["records"])
    # live == reload.
    assert "reasoning" in _persisted_text_fields(facts["client"], facts["sid"])


def test_synthesis_answer_stays_visible(tmp_path, monkeypatch) -> None:
    """synthesis is chain_of_thought and its ``answer`` is the user-facing answer;
    it must still stream as a visible text part."""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="synthesis",
        kind="chain_of_thought",
        deltas=[("answer", ANSWER)],
        visible_answer_stream=True,
        wait_field="answer",
    )
    visible = _visible_parts(facts["transcript"], "answer")
    assert len(visible) == 1, "synthesis answer must stay a visible text part"
    assert visible[0].text.strip()
    assert "react_extract_field_suppressed" not in _reasons(facts["records"])
    assert "answer" in _persisted_text_fields(facts["client"], facts["sid"])


# --------------------------------------------------------------------------- #
# THE FIX: kind: react EXTRACT reasoning + answer are suppressed.             #
# --------------------------------------------------------------------------- #


def test_react_extract_reasoning_suppressed_with_workflow_state(tmp_path, monkeypatch) -> None:
    """geospatial-shape (declares workflow_state -> answer already tap-suppressed):
    the LEAK is the extract ``reasoning``. It must NOT become a visible part, and a
    react_extract_field_suppressed reason is recorded. Live == reload."""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="geospatial",
        kind="react",
        # workflow_state expert -> answer stream not visible.
        deltas=[("reasoning", REASONING), ("answer", ANSWER)],
        visible_answer_stream=False,
    )
    assert _visible_parts(facts["transcript"], "reasoning") == []
    assert _visible_parts(facts["transcript"], "answer") == []
    reasons = _reasons(facts["records"])
    # Both fields suppressed under the react reason (one per delta).
    assert reasons.count("react_extract_field_suppressed") == 2
    # live == reload: neither field persisted as a visible part.
    persisted = _persisted_text_fields(facts["client"], facts["sid"])
    assert "reasoning" not in persisted
    assert "answer" not in persisted


def test_react_extract_answer_suppressed_without_workflow_state(tmp_path, monkeypatch) -> None:
    """A react expert with NO workflow_state has visible_answer_stream=True, so its
    answer WOULD stream today. The kind gate suppresses it (its VALUE flows to the
    delegation return contract), and records the reason. (test-fixture 1a)"""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="ndp_no_ws",
        kind="react",
        deltas=[("answer", ANSWER)],
        visible_answer_stream=True,
    )
    assert _visible_parts(facts["transcript"], "answer") == []
    assert "react_extract_field_suppressed" in _reasons(facts["records"])
    assert "answer" not in _persisted_text_fields(facts["client"], facts["sid"])


def test_react_next_thought_stays_visible(tmp_path, monkeypatch) -> None:
    """The react loop's per-step ``next_thought`` is the expert's visible
    conversation and must NOT be suppressed (no #732/#883 regression)."""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="geospatial",
        kind="react",
        deltas=[("next_thought", NEXT)],
        wait_field="next_thought",
    )
    visible = _visible_parts(facts["transcript"], "next_thought")
    assert len(visible) == 1
    assert visible[0].text.strip()
    assert "next_thought" in _persisted_text_fields(facts["client"], facts["sid"])


def test_react_kind_unresolved_records_reason_and_does_not_suppress(tmp_path, monkeypatch) -> None:
    """No-silent-fallback: a react scope active but kind unresolved records the
    react_kind_unresolved reason and does NOT suppress (safe CoT-visible behavior),
    so a resolution miss surfaces at the seam rather than silently leaking/deleting."""

    facts = _drive(
        tmp_path,
        monkeypatch,
        scope="geospatial",
        kind=None,  # scope set, kind NOT set
        deltas=[("reasoning", REASONING)],
        wait_field="reasoning",
    )
    # Safe fallback: reasoning stays visible (not deleted).
    assert len(_visible_parts(facts["transcript"], "reasoning")) == 1
    reasons = _reasons(facts["records"])
    assert "react_kind_unresolved" in reasons
    # It was NOT force-suppressed as a react extract.
    assert "react_extract_field_suppressed" not in reasons


# --------------------------------------------------------------------------- #
# Pure-unit guards: the shared gate truth table + context reset LIFO ordering. #
# --------------------------------------------------------------------------- #


def test_react_extract_field_suppressed_truth_table() -> None:
    """The single shared gate both seams call. Only ``react`` is gated; ``reasoning``
    always suppressed; ``answer`` suppressed unless it is the top-level deliverable;
    ``next_thought`` and every non-react kind stay visible (the landmine guard)."""

    from clio_agent.gact.context import react_extract_field_suppressed as gate

    # react, nested (answer not the live deliverable): both extract fields dropped.
    assert gate("react", "reasoning", answer_is_deliverable=False) is True
    assert gate("react", "answer", answer_is_deliverable=False) is True
    assert gate("react", "next_thought", answer_is_deliverable=False) is False
    # react, top-level deliverable stream: reasoning dropped, answer KEPT.
    assert gate("react", "reasoning", answer_is_deliverable=True) is True
    assert gate("react", "answer", answer_is_deliverable=True) is False
    # chain_of_thought / predict / off-scope: NEVER suppressed (the reverted-attempt
    # guard — reasoning is these experts' entire visible conversation).
    for kind in ("chain_of_thought", "predict", ""):
        for field in ("reasoning", "answer", "next_thought"):
            assert gate(kind, field, answer_is_deliverable=False) is False
            assert gate(kind, field, answer_is_deliverable=True) is False


def test_react_kind_context_set_and_reverse_lifo_reset() -> None:
    """react_kind threads through the single-var context and its reset restores the
    precise prior layer when unwound in reverse-LIFO with the scope/session tokens
    (the misordered-reset risk the design flags)."""

    from clio_agent.gact import context as ctx

    assert ctx.active_react_kind() == ""
    scope_tok = ctx.set_react_scope("geospatial")
    kind_tok = ctx.set_react_kind("react")
    session_tok = ctx.set_react_session("sess_x")
    assert ctx.active_react_kind() == "react"
    assert ctx.active_react_scope() == "geospatial"
    # Reverse-LIFO unwind (session -> kind -> scope), as both forward sites do.
    ctx.reset(session_tok)
    ctx.reset(kind_tok)
    assert ctx.active_react_kind() == ""  # restored to pre-set layer
    assert ctx.active_react_scope() == "geospatial"  # scope still set (below kind)
    ctx.reset(scope_tok)
    assert ctx.active_react_scope() == ""


def test_stream_listeners_answer_only() -> None:
    """#878 seam guard: `_build_stream_listeners` binds ONLY `answer`-field
    listeners, so the streamify `_emit_visible_chunk` seam never receives a react
    EXTRACT `reasoning` field. If a future change binds a `reasoning` listener,
    this fails — the signal to add a top-level kind gate at that seam."""

    from clio_agent.gact.streaming import _build_stream_listeners

    class _CaptureListener:
        def __init__(self, *, signature_field_name: str, predict) -> None:
            self.signature_field_name = signature_field_name
            self.predict = predict

    class _Predictor:
        pass

    class _FakeAgent:
        def __init__(self) -> None:
            self.program = _Predictor()
            self.react_agent = _Predictor()
            self.answer_synthesizer = _Predictor()

    listeners = _build_stream_listeners(_FakeAgent(), _CaptureListener)
    assert listeners, "expected at least one bound listener"
    assert {ls.signature_field_name for ls in listeners} == {"answer"}
