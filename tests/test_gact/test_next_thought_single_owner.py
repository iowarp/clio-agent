"""E2E single-representation guard for the ReAct ``next_thought`` (#732 / S2).

The ReAct step's ``next_thought`` has TWO potential homes on the wire: a VISIBLE
streamed text row (``note_lm_answer_delta`` -> ``emit_chunk`` ->
``append_text_delta``, a ``text`` part tagged ``signature_field_name="next_thought"``)
and a redundant COPY the tool observer stamps onto ``tool_call.thought``. The
approved render model gives the visible row sole ownership, so the observer must
clear the tool_call copy IFF that visible row exists — and KEEP it when it does
not (the SDK/batch gap), or the thought VANISHES (renders zero times).

This drives the REAL interaction the first S2 attempt's mocked test missed:
- the real ``note_lm_answer_delta`` records the transcript's real tap
  (``record_streamed_field_text``) synchronously, then schedules the visible emit
  onto a real loop through the real ``emit_chunk`` adapter;
- the real ``_make_tool_observer`` gate reads the tap via the op-identity
  ``streamed_field_started`` presence check and builds the real ``tool_call`` part.
No ``record_dedup`` mock, no hand-crafted ``tool_call.thought``.

Scenario A (streaming): the visible row exists -> the tool_call copy is cleared;
next_thought renders EXACTLY once (the visible text part). Scenario B (SDK/batch
gap): no visible row -> the tool_call copy is KEPT; next_thought renders EXACTLY
once (on the tool call). Together they pin single-representation on both
transports; scenario B is the exact content-loss blocker attempt 1 hit.

Beyond the live transcript, each scenario also PERSISTS its settled parts and
reloads them through ``GET /v1/sessions/{sid}/messages`` (must-fix 3): the read
boundary must preserve single-representation. A hand-persisted OLD-shape fixture
(a message carrying BOTH a next_thought text row and a populated
tool_call.thought, the pre-S2 on-disk shape) characterizes + guards the
historical-reload normalization (must-fix 1).

No-silent-fallback assertions read a directly-controlled audit sink: the tests
monkeypatch ``stream_audit`` at every module that emits it (must-fix 2). The prior
revision read the conf-resolved ``CLIO_STREAM_AUDIT_LOG`` file, which flaked — a
process-global ``conf._STORE`` file-layer cache can defeat ``monkeypatch.setenv``
(conf precedence is file -> env -> default), landing the record on a different
path than the test reads. Patching the sink removes conf + filesystem from the
assertion entirely, so it is deterministic under any test order or RNG seed.

Two gate properties the tool_observer comment references live here (they are
what these scenarios exercise, so the rationale sits with the driver):

* Agent-id identity across the three sites. The tap records dedup presence under
  ``active_react_scope()`` (``lm_activity.note_lm_answer_delta``); the visible
  ``text`` part is stamped with that same scope (its chunk agent); and the gate +
  the ``tool_call`` it emits both use ``invoking_expert = active_react_scope() or
  tool_owner``. A ``next_thought`` visible row is only ever produced while a react
  scope is active, and ``record_dedup`` is skipped off-scope — so whenever a row
  exists all three keys are the SAME string, and the ``or tool_owner`` fallback
  only bites off-scope, where no row was recorded anyway. Scenario A drives this:
  the same ``REACT_SCOPE`` tags the streamed text row, the tap, and the cleared
  tool_call, and the clear only fires because those keys coincide.

* Turn-scope latch soundness (why per-step scoping is deferred, an S2-review LOW).
  ``streamed_field_started`` reads presence accumulated per ``(agent, field)`` over
  the WHOLE turn, not per step. A mixed turn — step 1 streams its ``next_thought``,
  step 2 does not — would wrongly clear step 2's distinct thought. That shape is
  not real: DSPy ReAct emits a ``next_thought`` every step under a uniform
  transport, so a turn either streams all of them (scenario A: each owns its row,
  each copy cleared) or none (scenario B: presence False, all kept). A within-turn
  visible/non-visible mix does not occur, so step-scoping — which would thread a
  per-step counter through the cross-thread tap and add a race to the
  happens-before path — is deferred deliberately.
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
from clio_agent.gact.agents.resolution import _active_workflow_state_schema
from clio_agent.gact.app import build_app
from clio_agent.gact.tool_observer import _make_tool_observer, _open_turn_transcript
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.turn_stream import emit_chunk
from clio_agent.gact.types import Message, Part
from clio_agent.runtime.lm_activity import note_lm_answer_delta, set_live_chunk_emitter

# The clean next_thought the model emits for its geocode step. Plain prose, so the
# transcript's public-text cleaner keeps it verbatim.
NEXT = (
    "The user asked to resolve the place name Los Angeles to a geographic region, "
    "so I call geo_geocode to look it up."
)
# The raw DSPy reasoning channel (marker-laden) — never the tool_call's home.
RAW_REASONING = f"```[[ ## next_thought ## ]]\n{NEXT}"

REACT_SCOPE = "geospatial"

# The stage every next_thought/tool_call single-representation record carries,
# emitted by BOTH the live observer gate and the read-boundary normalizer.
_TOOL_THOUGHT_STAGE = "bridge.tool_thought"

# Every module that binds ``stream_audit`` by name and emits it on a path these
# tests exercise. Patching each binding (``from ... import stream_audit`` copies
# the reference) routes the reason into an in-process list — no conf, no file.
_AUDIT_SINK_TARGETS = (
    "clio_agent.gact.tool_observer.stream_audit",
    "clio_agent.gact.message_wire.stream_audit",
    "clio_agent.runtime.lm_activity.stream_audit",
)


def _install_audit_capture(monkeypatch) -> list[dict]:
    """Patch every ``stream_audit`` binding to append into a returned list.

    Deterministic replacement for reading the conf-resolved audit FILE (must-fix
    2): the assertions verify the code CALLED ``stream_audit`` with the structured
    reason, independent of conf precedence or filesystem state.
    """

    records: list[dict] = []

    def _capture(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    for target in _AUDIT_SINK_TARGETS:
        monkeypatch.setattr(target, _capture)
    return records


def _visible_next_thought_parts(transcript) -> list:
    """Ledger ``text`` parts that represent the visible next_thought row."""

    return [
        p
        for p in transcript.snapshot()
        if p.type == "text" and p.metadata.get("signature_field_name") == "next_thought"
    ]


def _tool_call_parts(transcript) -> list:
    return [p for p in transcript.snapshot() if p.type == "tool_call"]


def _wait_for_visible_part(transcript, *, timeout: float = 5.0) -> None:
    """Block until the scheduled ``emit_chunk`` has landed the visible text part."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _visible_next_thought_parts(transcript):
            return
        time.sleep(0.01)


def _tool_thought_reasons(records: list[dict]) -> list[str]:
    """The ``duplicate_reason`` of every ``bridge.tool_thought`` record captured."""

    return [
        r.get("duplicate_reason")
        for r in records
        if r.get("stage") == _TOOL_THOUGHT_STAGE
    ]


def _run_executor_work(work) -> None:
    """Run ``work`` on a dedicated (executor-like) thread with an isolated context.

    The tap + observer run in an executor thread in production; running them off
    the main thread here keeps the runtime contextvars they set from leaking
    across scenarios, and reproduces the cross-thread scheduling onto the loop.
    """

    err: list[BaseException] = []

    def _target() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - surface to the test thread
            err.append(exc)

    th = threading.Thread(target=_target)
    th.start()
    th.join(15)
    assert not th.is_alive(), "executor work thread hung"
    if err:
        raise err[0]


def _persist_message(app, sid: str, parts: list[Part], *, msg_id: str, turn_id: str) -> str:
    """Append a settled assistant message to the ledger (in-memory + store)."""

    now = datetime.now(timezone.utc).isoformat()
    msg = Message(
        id=msg_id,
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
    return msg_id


def _reload_tool_call(client: TestClient, sid: str, msg_id: str) -> dict:
    """Fetch ``msg_id`` back through GET /messages and return its tool_call wire."""

    resp = client.get(f"/v1/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    msg = next((m for m in body["messages"] if m["id"] == msg_id), None)
    assert msg is not None, f"message {msg_id} not returned on reload"
    tool_calls = [p for p in msg["parts"] if p.get("type") == "tool_call"]
    assert len(tool_calls) == 1, f"expected exactly one tool_call, got {tool_calls}"
    return tool_calls[0]


def _drive_scenario(
    tmp_path: Path,
    monkeypatch,
    *,
    stream_next_thought: bool,
) -> dict:
    """Build a real app + ledger + observer and drive the real tap/observer path.

    Returns the settled ledger facts + the captured stream_audit records, and the
    live ``app``/``client``/``sid`` so the caller can persist + reload.
    """

    records = _install_audit_capture(monkeypatch)

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    sess = app.state.sessions.get(sid)
    schema = _active_workflow_state_schema(app, sid)
    turn_id = "turn_s2"
    trace_id = "trace_s2"
    transcript = _open_turn_transcript(app, sid, turn_id, schema=schema)

    # A real event loop running in a background thread — the tap schedules the
    # visible emit onto it cross-thread exactly like the turn loop.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    def work() -> None:
        _ctx.set_turn_identity(app=app, session_id=sid, turn_id=turn_id, trace_id=trace_id)
        _ctx.set_tool_session_id(sid)
        _ctx.set_react_scope(REACT_SCOPE)
        _ctx.set_visible_answer_stream(True)

        state = TurnState(
            app=app,
            sid=sid,
            user_text="",
            user_msg=sess,  # emit_chunk never reads it
            turn_agent_id=REACT_SCOPE,
            sess=sess,
            bus=app.state.bus,
            turn_id=turn_id,
            trace_id=trace_id,
            retry_attempt_id="",
            native_images=[],
        )
        state.transcript = transcript
        state.active_agent_id = REACT_SCOPE
        state.invocation_agent_id = REACT_SCOPE

        # Bind the live emitter with the REAL synchronous tap recorder — exactly
        # as turn_stream.bind_live_emitter does.
        set_live_chunk_emitter(
            loop,
            partial(emit_chunk, state),
            transcript.record_streamed_field_text,
        )

        if stream_next_thought:
            # Streaming path: the REAL tap records (agent, next_thought) in-thread
            # and schedules the visible emit onto the loop.
            note_lm_answer_delta(NEXT, field="next_thought")
            _wait_for_visible_part(transcript)

        # The ReAct step's thought is on the runtime context; the observer reads it.
        _ctx.set_step_thought(NEXT, RAW_REASONING)
        observer = _make_tool_observer(app)
        observer("geo_geocode", {"place": "Los Angeles"}, "started", None)

    try:
        _run_executor_work(work)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(5)
        loop.close()

    visible = _visible_next_thought_parts(transcript)
    tool_calls = _tool_call_parts(transcript)
    return {
        "app": app,
        "client": client,
        "sid": sid,
        "turn_id": turn_id,
        "transcript": transcript,
        "visible": visible,
        "tool_calls": tool_calls,
        "records": records,
        "audit_reasons": _tool_thought_reasons(records),
    }


class _StubAgent:
    """A minimal agent that cannot resolve a tool owner (so the observer emits no
    route banner) — the tool_call is attributed to the active react scope."""

    def forward(self, question: str, session_id: str):  # pragma: no cover - unused
        raise NotImplementedError


def test_streaming_next_thought_owns_visible_row_and_clears_tool_copy(tmp_path, monkeypatch) -> None:
    """Scenario A: the visible row exists -> the tool_call copy is cleared; the
    next_thought is represented EXACTLY once (its visible text part) — live AND on
    persist->reload."""

    facts = _drive_scenario(tmp_path, monkeypatch, stream_next_thought=True)

    visible = facts["visible"]
    tool_calls = facts["tool_calls"]

    # The visible next_thought text row was emitted (its own part).
    assert len(visible) == 1
    assert visible[0].text.strip()  # closed with the cleaned prose, not dropped

    # Exactly one tool_call, and its thought is CLEARED (the redundant copy).
    assert len(tool_calls) == 1
    assert (tool_calls[0].thought or "") == ""

    # Single representation: (visible rows) + (tool_calls carrying a thought) == 1.
    non_empty_tool_thoughts = [tc for tc in tool_calls if (tc.thought or "").strip()]
    assert len(visible) + len(non_empty_tool_thoughts) == 1

    # No-silent-fallback: the suppressed channel emitted a structured reason.
    assert "next_thought_owns_visible_text_row" in facts["audit_reasons"]

    # must-fix 3: persist the settled parts and reload — single representation must
    # survive the read boundary (the tool_call thought stays cleared).
    msg_id = _persist_message(
        facts["app"],
        facts["sid"],
        facts["transcript"].snapshot(),
        msg_id="msg_A",
        turn_id=facts["turn_id"],
    )
    reloaded = _reload_tool_call(facts["client"], facts["sid"], msg_id)
    assert reloaded.get("thought", "") == ""


def test_sdk_gap_next_thought_kept_on_tool_call_no_content_loss(tmp_path, monkeypatch) -> None:
    """Scenario B: no visible row (SDK/batch gap) -> the tool_call copy is KEPT; the
    next_thought is represented EXACTLY once (on the tool call) — live AND on
    persist->reload. This is the exact content-loss blocker the first S2 attempt
    hit (an unconditional clear -> 0)."""

    facts = _drive_scenario(tmp_path, monkeypatch, stream_next_thought=False)

    visible = facts["visible"]
    tool_calls = facts["tool_calls"]

    # No visible next_thought row was streamed.
    assert len(visible) == 0

    # The tool_call KEEPS the thought — its only home this turn.
    assert len(tool_calls) == 1
    assert (tool_calls[0].thought or "").strip() == NEXT

    # Single representation: exactly one home, on the tool call.
    non_empty_tool_thoughts = [tc for tc in tool_calls if (tc.thought or "").strip()]
    assert len(visible) + len(non_empty_tool_thoughts) == 1

    # No-silent-fallback: the KEEP path emitted its structured reason.
    assert "thought_kept_no_visible_row" in facts["audit_reasons"]

    # must-fix 3: persist + reload — with no visible row, the read boundary must
    # NOT clear the tool_call thought (it is the thought's only home).
    msg_id = _persist_message(
        facts["app"],
        facts["sid"],
        facts["transcript"].snapshot(),
        msg_id="msg_B",
        turn_id=facts["turn_id"],
    )
    reloaded = _reload_tool_call(facts["client"], facts["sid"], msg_id)
    assert reloaded.get("thought", "") == NEXT


def _old_shape_parts(*, with_visible_row: bool) -> list[Part]:
    """Pre-S2 on-disk shape: a tool_call whose thought was NOT cleared at write.

    ``with_visible_row`` toggles the sibling next_thought text row that the
    read-boundary op-identity gate keys off of.
    """

    parts: list[Part] = []
    if with_visible_row:
        parts.append(
            Part(
                id="p_text",
                type="text",
                agent_id=REACT_SCOPE,
                text=NEXT,
                metadata={"stream_source": "live", "signature_field_name": "next_thought"},
            )
        )
    parts.append(
        Part(
            id="p_call",
            type="tool_call",
            agent_id=REACT_SCOPE,
            call_id="call_old",
            tool_name="geo_geocode",
            thought=NEXT,  # the redundant pre-S2 copy that must be repaired on read
            input={"place": "Los Angeles"},
            metadata={"stream_source": "live"},
        )
    )
    return parts


def test_historical_reload_clears_redundant_tool_thought(tmp_path, monkeypatch) -> None:
    """must-fix 1: a pre-S2 persisted message carrying BOTH a next_thought text row
    and a populated tool_call.thought reloads with the redundant copy CLEARED
    (op-identity presence, not a string compare), and emits the read-boundary
    stream_audit reason (no silent drop)."""

    records = _install_audit_capture(monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    msg_id = _persist_message(
        app,
        sid,
        _old_shape_parts(with_visible_row=True),
        msg_id="msg_old_both",
        turn_id="turn_old",
    )

    reloaded = _reload_tool_call(client, sid, msg_id)
    # The redundant copy is cleared; the visible text row remains the sole home.
    assert reloaded.get("thought", "") == ""

    resp = client.get(f"/v1/sessions/{sid}/messages")
    msg = next(m for m in resp.json()["messages"] if m["id"] == msg_id)
    visible = [
        p
        for p in msg["parts"]
        if p.get("type") == "text"
        and p.get("metadata", {}).get("signature_field_name") == "next_thought"
    ]
    assert len(visible) == 1 and visible[0].get("text", "").strip() == NEXT

    reasons = _tool_thought_reasons(records)
    assert "next_thought_owns_visible_text_row" in reasons
    read_records = [
        r
        for r in records
        if r.get("stage") == _TOOL_THOUGHT_STAGE and r.get("origin") == "message_read"
    ]
    assert read_records, "read-boundary clear must emit origin=message_read audit"


def test_historical_reload_without_visible_row_keeps_tool_thought(tmp_path, monkeypatch) -> None:
    """must-fix 1 (KEEP half): a persisted message with a populated
    tool_call.thought but NO sibling next_thought text row is a scenario-B shape —
    the read boundary must leave the thought untouched (its only home)."""

    records = _install_audit_capture(monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    msg_id = _persist_message(
        app,
        sid,
        _old_shape_parts(with_visible_row=False),
        msg_id="msg_old_keep",
        turn_id="turn_old",
    )

    reloaded = _reload_tool_call(client, sid, msg_id)
    assert reloaded.get("thought", "") == NEXT

    # No visible row -> the read-boundary normalizer emits nothing (no clear).
    assert "next_thought_owns_visible_text_row" not in _tool_thought_reasons(records)
