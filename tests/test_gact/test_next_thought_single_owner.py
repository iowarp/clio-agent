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
- the real ``_make_tool_observer`` gate reads the tap via the per-step
  ``tap_step_survives_clean`` predicate and builds the real ``tool_call`` part.
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

* Per-step latch soundness (#883, formerly deferred as an S2-review LOW). The gate
  now consumes a PER-STEP tap slice via ``tap_step_survives_clean``: a ``(agent,
  field)`` cursor carves the append-only tap bucket into ReAct steps, so a mixed
  turn — step 1 streams surviving ``next_thought`` prose, step 2 streams a
  marker-only ``next_thought`` that cleans to empty — clears step 1's copy but
  KEEPS step 2's (``test_multistep_marker_only_step2_kept``). The old whole-turn
  ``streamed_field_started`` latch would have cleared step 2 too — the exact #883
  marker-only vanish this file's central sabotage test now pins.
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
from clio_agent.gact.message_wire import normalize_thought_ownership
from clio_agent.gact.thought_dedup import read_boundary_clean, survives_clean
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

# A next_thought that streams ONLY a ChatAdapter field marker (no surviving prose)
# — it cleans to empty, so its visible row is dropped at close. This is the #883
# marker-only vanish class. NOTE: the spec's ``"```[[ ## next_thought ## ]]\n"`` does
# NOT clean to empty (the real transcript cleaner keeps a bare leading ``` fence), so
# we use the bare marker, which ``_clean_public_transcript_text`` verifiably empties.
MARKER_ONLY = "[[ ## next_thought ## ]]\n"

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
    "clio_agent.gact.transcript.stream_audit",
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


def _wait_for_next_thought_count(transcript, count: int, *, timeout: float = 5.0) -> None:
    """Block until the ledger holds at least ``count`` next_thought text parts.

    A per-step wait: the cross-thread ``emit_chunk`` opens a new next_thought part
    for the NEXT streamed step; waiting for the count to grow ensures that open
    part exists before the step's tool-fire closes it (else the close races the
    add and an orphan open part leaks past the tool_call).
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_visible_next_thought_parts(transcript)) >= count:
            return
        time.sleep(0.01)


def _tool_thought_reasons(records: list[dict]) -> list[str]:
    """The ``duplicate_reason`` of every ``bridge.tool_thought`` record captured."""

    return [r.get("duplicate_reason") for r in records if r.get("stage") == _TOOL_THOUGHT_STAGE]


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
    chunk_text: str = NEXT,
    step2: dict | None = None,
) -> dict:
    """Build a real app + ledger + observer and drive the real tap/observer path.

    ``chunk_text`` is the next_thought tap chunk streamed for step 1 (default the
    surviving ``NEXT`` prose; pass ``MARKER_ONLY`` for the cleans-to-empty case).
    ``step2`` optionally drives a SECOND ReAct step on the same agent — a dict
    ``{"stream": str|None, "tool": name, "thought": str, "args": {...}}`` — so a
    within-turn mixed transport (step1 survives, step2 marker-only) is exercised.

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

        observer = _make_tool_observer(app)

        if stream_next_thought:
            # Streaming path: the REAL tap records (agent, next_thought) in-thread
            # and schedules the visible emit onto the loop.
            note_lm_answer_delta(chunk_text, field="next_thought")
            if chunk_text.strip() and chunk_text != MARKER_ONLY:
                _wait_for_visible_part(transcript)
            else:
                # Marker-only: the row is added (open) then dropped at close. Wait
                # for the open part to exist so the tool-fire close is not raced.
                _wait_for_next_thought_count(transcript, 1)

        # The ReAct step's thought is on the runtime context; the observer reads it.
        # A per-step parent span (set per-step in production at agents/runtime.py)
        # stamps the audit's step_id label.
        _ctx.set_parent_span("span_step1")
        _ctx.set_step_thought(NEXT, RAW_REASONING)
        observer("geo_geocode", {"place": "Los Angeles"}, "started", None)

        if step2 is not None:
            prior = len(_visible_next_thought_parts(transcript))
            stream2 = step2.get("stream")
            if stream2 is not None:
                note_lm_answer_delta(stream2, field="next_thought")
                _wait_for_next_thought_count(transcript, prior + 1)
            _ctx.set_parent_span("span_step2")
            _ctx.set_step_thought(step2["thought"], step2.get("thought", ""))
            observer(step2["tool"], step2.get("args", {}), "started", None)

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


def test_streaming_next_thought_owns_visible_row_and_clears_tool_copy(
    tmp_path, monkeypatch
) -> None:
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


def _tool_call_by_name(transcript, name: str):
    """The single tool_call part in the ledger for ``name`` (asserts uniqueness)."""

    calls = [p for p in _tool_call_parts(transcript) if p.tool_name == name]
    assert len(calls) == 1, f"expected one {name} tool_call, got {len(calls)}"
    return calls[0]


def test_multistep_marker_only_step2_kept(tmp_path, monkeypatch) -> None:
    """Case (c): one agent, two steps. Step 1 streams surviving prose + fires
    geo_geocode (its copy is CLEARED); step 2 streams a marker-only next_thought
    that cleans to empty + fires geo_bbox with a distinct thought (its copy is
    KEPT — the #883 vanish class). Exactly one visible row survives; live == reload.
    """

    step2_thought = "Now compute its bounding box"
    facts = _drive_scenario(
        tmp_path,
        monkeypatch,
        stream_next_thought=True,
        chunk_text=NEXT,
        step2={"stream": MARKER_ONLY, "tool": "geo_bbox", "thought": step2_thought},
    )

    transcript = facts["transcript"]
    # Step 1's prose survives as the sole visible next_thought row; step 2's
    # marker-only row was dropped at close.
    assert len(facts["visible"]) == 1

    step1 = _tool_call_by_name(transcript, "geo_geocode")
    step2 = _tool_call_by_name(transcript, "geo_bbox")
    assert (step1.thought or "") == ""  # cleared: it owns the surviving row
    assert (step2.thought or "").strip() == step2_thought  # KEPT: no surviving row

    reasons = facts["audit_reasons"]
    assert "next_thought_owns_visible_text_row" in reasons
    assert "thought_kept_next_thought_cleaned_empty" in reasons

    # Persist the settled ledger and reload: the read boundary must reproduce the
    # SAME per-step ownership (step1 cleared, step2 kept).
    msg_id = _persist_message(
        facts["app"],
        facts["sid"],
        transcript.snapshot(),
        msg_id="msg_C",
        turn_id=facts["turn_id"],
    )
    resp = facts["client"].get(f"/v1/sessions/{facts['sid']}/messages")
    msg = next(m for m in resp.json()["messages"] if m["id"] == msg_id)
    calls = {p["tool_name"]: p for p in msg["parts"] if p.get("type") == "tool_call"}
    assert calls["geo_geocode"].get("thought", "") == ""
    assert calls["geo_bbox"].get("thought", "") == step2_thought


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


# --- case (b): marker-only single step, live --------------------------------


def test_marker_only_next_thought_kept_live(tmp_path, monkeypatch) -> None:
    """Case (b): a step whose next_thought streams a marker that cleans to empty
    KEEPS its tool_call.thought (the #883 vanish class). The visible row is dropped,
    a transcript.dropped_empty_part fires, and the KEEP audit carries a step_id."""

    facts = _drive_scenario(tmp_path, monkeypatch, stream_next_thought=True, chunk_text=MARKER_ONLY)

    assert len(facts["visible"]) == 0  # marker-only row dropped at close
    tool_calls = facts["tool_calls"]
    assert len(tool_calls) == 1
    assert (tool_calls[0].thought or "").strip() == NEXT  # KEPT

    reasons = facts["audit_reasons"]
    assert "thought_kept_next_thought_cleaned_empty" in reasons
    keep = [
        r
        for r in facts["records"]
        if r.get("duplicate_reason") == "thought_kept_next_thought_cleaned_empty"
    ]
    assert keep and keep[0].get("duplicate_suppressed") is False
    assert keep[0].get("step_id") == "span_step1"  # audit label populated

    # The visible row was dropped from the ledger (empty after clean).
    dropped = [r for r in facts["records"] if r.get("stage") == "transcript.dropped_empty_part"]
    assert dropped, "marker-only next_thought row must drop at close"

    # Persist + reload: the thought stays on the tool_call (its only home).
    msg_id = _persist_message(
        facts["app"],
        facts["sid"],
        facts["transcript"].snapshot(),
        msg_id="msg_b1",
        turn_id=facts["turn_id"],
    )
    reloaded = _reload_tool_call(facts["client"], facts["sid"], msg_id)
    assert reloaded.get("thought", "") == NEXT


# --- transcript predicate unit tests ----------------------------------------


def _make_transcript(app, sid):
    """Build a real TurnTranscript with the app's schema-bound clean_text."""

    from clio_agent.gact.delegation import _clean_public_transcript_text
    from clio_agent.gact.transcript import (
        EventBusTranscriptPublisher,
        TurnTranscript,
    )

    schema = _active_workflow_state_schema(app, sid)
    return TurnTranscript(
        session_id=sid,
        turn_id="t_unit",
        publisher=EventBusTranscriptPublisher(app.state.bus, sid),
        clean_text=lambda text: _clean_public_transcript_text(
            text, schema=schema, preserve_whitespace=True
        ),
    )


def _fresh_app(tmp_path):
    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    return app, client, sid


def test_double_gate_call_is_guarded_noop(tmp_path) -> None:
    """7.6: tap_step_survives_clean is a CONSUMING read. A second call for the same
    streamed step sees an empty slice -> (False, False) -> KEEP. The observer is the
    sole call site (grep-pinned), so a real turn calls it exactly once per tool-fire."""

    app, _client, sid = _fresh_app(tmp_path)
    t = _make_transcript(app, sid)
    t.record_streamed_field_text(REACT_SCOPE, "next_thought", NEXT)

    first = t.tap_step_survives_clean(REACT_SCOPE, "next_thought")
    second = t.tap_step_survives_clean(REACT_SCOPE, "next_thought")
    assert first == (True, True)
    assert second == (False, False)  # slice consumed -> KEEP, never a stale re-clear


def test_survives_clean_equals_drop_predicate(tmp_path) -> None:
    """7.7: the gate's survival bit is byte-for-byte the close-time drop predicate.
    For each text, tap_step_survives_clean's survival == bool(_clean_text().strip()),
    and driving a REAL close drops the row iff survival is False."""

    app, _client, sid = _fresh_app(tmp_path)
    t = _make_transcript(app, sid)
    cases = [
        ("prose", "The user asked to geocode Los Angeles."),
        ("marker", MARKER_ONLY),
        ("whitespace", "   \n  "),
        ("mixed", f"{MARKER_ONLY}kept prose"),
    ]
    for agent, text in cases:
        t.record_streamed_field_text(agent, "next_thought", text)
        _had, survived = t.tap_step_survives_clean(agent, "next_thought")
        assert survived == bool(t._clean_text(text).strip()), f"predicate drift on {agent!r}"


def test_survives_clean_close_drop_agreement(tmp_path) -> None:
    """7.7 (real close): a marker-only row is DROPPED by the real close while a prose
    row SURVIVES — the drop condition the gate's survival bit mirrors."""

    app, _client, sid = _fresh_app(tmp_path)
    t = _make_transcript(app, sid)
    t.append_text_delta("a1", "next_thought", "real prose survives")
    t.append_text_delta("a2", "next_thought", MARKER_ONLY)  # boundary closes a1
    parts = t.finalize()
    kept = [p for p in parts if p.type == "text"]
    assert len(kept) == 1 and kept[0].text.strip() == "real prose survives"


def test_marker_split_across_chunks(tmp_path) -> None:
    """7.8: a marker split across two tap chunks must be JOINED before cleaning, so
    the gate sees (True, False) -> KEEP (cleaning per-chunk would see the first half
    as non-empty -> CLEAR -> vanish)."""

    app, _client, sid = _fresh_app(tmp_path)
    t = _make_transcript(app, sid)
    t.record_streamed_field_text(REACT_SCOPE, "next_thought", "[[ ## next_")
    t.record_streamed_field_text(REACT_SCOPE, "next_thought", "thought ## ]]\n")

    had, survived = t.tap_step_survives_clean(REACT_SCOPE, "next_thought")
    assert had is True
    assert survived is False  # the JOINED slice cleans to empty
    # Close agrees: the joined buffer cleans to empty.
    assert not t._clean_text("[[ ## next_thought ## ]]\n").strip()


# --- pre-S2 reload fixtures + tests ------------------------------------------


def _msg(parts: list[Part]) -> Message:
    now = datetime.now(timezone.utc).isoformat()
    return Message(
        id="m_reload",
        session_id="s",
        turn_id="turn_old",
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=parts,
    )


def _text_row(text: str, *, agent: str = REACT_SCOPE, pid: str = "row") -> Part:
    return Part(
        id=pid,
        type="text",
        agent_id=agent,
        text=text,
        metadata={"stream_source": "live", "signature_field_name": "next_thought"},
    )


def _call(thought: str, *, agent: str = REACT_SCOPE, pid: str, tool: str = "geo_geocode") -> Part:
    return Part(
        id=pid,
        type="tool_call",
        agent_id=agent,
        call_id=pid,
        tool_name=tool,
        thought=thought,
        input={},
        metadata={"stream_source": "live"},
    )


def test_reload_pre_s2_multistep_matches_live(tmp_path, monkeypatch) -> None:
    """7.3 (case d): a pre-S2 two-step message (step1 row + copy c1; step2 NO row +
    copy c2, same agent). Per-step consume clears c1 (its row owns it) and KEEPS c2
    (its step streamed no surviving row) — the set-based logic wrongly cleared c2."""

    _install_audit_capture(monkeypatch)
    msg = _msg(
        [
            _text_row(NEXT, pid="row1"),
            _call(NEXT, pid="c1", tool="geo_geocode"),
            _call("bbox reasoning", pid="c2", tool="geo_bbox"),
        ]
    )
    out = normalize_thought_ownership(msg)
    by = {p.id: p for p in out.parts}
    assert (by["c1"].thought or "") == ""  # cleared: owns row1
    assert (by["c2"].thought or "") == "bbox reasoning"  # KEPT: its step had no row


def test_reload_pre_s2_marker_only_row_kept(tmp_path, monkeypatch) -> None:
    """7.4 (case e): a pre-S2 RAW marker-only next_thought row cleans to empty via
    read_boundary_clean, so it does NOT falsely own the step — the sibling
    tool_call.thought is KEPT (a bare .strip() would over-clear it -> vanish)."""

    _install_audit_capture(monkeypatch)
    msg = _msg(
        [
            _text_row("[[ ## next_thought ## ]]", pid="rowm"),
            _call(NEXT, pid="cm", tool="geo_geocode"),
        ]
    )
    out = normalize_thought_ownership(msg)
    by = {p.id: p for p in out.parts}
    assert (by["cm"].thought or "") == NEXT  # marker row owns nothing -> KEPT


def test_reload_contract_sentence_only_residual_documented(tmp_path, monkeypatch) -> None:
    """7.5 (disclosed residual): a pre-S2 next_thought row of contract-sentence-only
    prose (NO markers) survives read_boundary_clean, so reload treats it as an owner
    and clears the sibling. This asserts the CURRENT behavior, not a wrong guarantee.

    KNOWN RESIDUAL (#883 follow-up): the read-boundary clean is schema-free and cannot
    empty contract-sentence-only pre-S2 rows the live schema-bound clean would empty.
    Fix requires persisting the pack-schema id and rebuilding the schema-bound clean
    at read time; tracked as a #883 follow-up, NOT silently swallowed.
    """

    _install_audit_capture(monkeypatch)
    # A non-marker sentence that read_boundary_clean keeps verbatim (it only strips
    # field markers), documenting the residual: reload over-clears the sibling.
    contract_sentence = "This is the workflow_state carrier prose."
    assert read_boundary_clean(contract_sentence) == contract_sentence
    msg = _msg(
        [
            _text_row(contract_sentence, pid="rowc"),
            _call(NEXT, pid="cc", tool="geo_geocode"),
        ]
    )
    out = normalize_thought_ownership(msg)
    by = {p.id: p for p in out.parts}
    assert (by["cc"].thought or "") == ""  # residual: over-cleared (documented)


def test_reload_marker_only_keep_audit(tmp_path, monkeypatch) -> None:
    """7.13: a meaningful reload KEEP (a rowless copy whose agent owned a row
    elsewhere) emits thought_kept_no_surviving_next_thought_row (origin=message_read);
    a plain no-row message emits NO reload KEEP audit (spam guard)."""

    records = _install_audit_capture(monkeypatch)
    msg = _msg(
        [
            _text_row(NEXT, pid="row1"),
            _call(NEXT, pid="c1", tool="geo_geocode"),
            _call("kept step2", pid="c2", tool="geo_bbox"),
        ]
    )
    normalize_thought_ownership(msg)
    keep = [
        r
        for r in records
        if r.get("duplicate_reason") == "thought_kept_no_surviving_next_thought_row"
        and r.get("origin") == "message_read"
    ]
    assert keep, "meaningful reload KEEP must be audited"

    # Spam guard: a plain no-row message (scenario B) emits no reload KEEP.
    records.clear()
    plain = _msg([_call(NEXT, pid="only", tool="geo_geocode")])
    normalize_thought_ownership(plain)
    assert not [
        r
        for r in records
        if r.get("duplicate_reason") == "thought_kept_no_surviving_next_thought_row"
    ]


def test_survives_clean_kernel_format_only() -> None:
    """The shared kernel is format-only: non-blank-after-clean, never prose-keyed."""

    assert survives_clean("real prose", read_boundary_clean) is True
    assert survives_clean("[[ ## next_thought ## ]]\n", read_boundary_clean) is False
    assert survives_clean("   ", read_boundary_clean) is False
    assert survives_clean("", read_boundary_clean) is False


def test_live_equals_reload_thought_field(tmp_path, monkeypatch) -> None:
    """7.14: drive the REAL streamed->persisted->normalize path for case (c) and
    assert the reloaded tool_call.thought equals the LIVE values field-for-field —
    the read-boundary transform reproduces live single-representation on the field
    #883 concerns (the conftest fold asserts the pre-normalize equality)."""

    step2_thought = "Now compute its bounding box"
    facts = _drive_scenario(
        tmp_path,
        monkeypatch,
        stream_next_thought=True,
        chunk_text=NEXT,
        step2={"stream": MARKER_ONLY, "tool": "geo_bbox", "thought": step2_thought},
    )
    live = {p.tool_name: (p.thought or "") for p in facts["tool_calls"]}
    assert live == {"geo_geocode": "", "geo_bbox": step2_thought}

    msg_id = _persist_message(
        facts["app"],
        facts["sid"],
        facts["transcript"].snapshot(),
        msg_id="msg_parity",
        turn_id=facts["turn_id"],
    )
    resp = facts["client"].get(f"/v1/sessions/{facts['sid']}/messages")
    msg = next(m for m in resp.json()["messages"] if m["id"] == msg_id)
    reload = {
        p["tool_name"]: p.get("thought", "") for p in msg["parts"] if p.get("type") == "tool_call"
    }
    assert reload == live  # read boundary == live, field-for-field
