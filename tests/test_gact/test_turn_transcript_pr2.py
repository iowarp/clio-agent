"""#767 PR2 — the turn loop owns the TurnTranscript lifecycle.

Covers the PR2 behavior surface:

* the frozen/never-minted ``ensure_message`` guard (structured error, never a
  silent empty-string id) — the PR1-verdict carry-over;
* ``abandon()`` (freeze-without-publish settle) + the new legacy-equivalent
  state queries the finalize region reads instead of the deleted ``turn.py``
  closure vars (``current_stream_part_id``, ``was_closed_live``,
  ``raw_streamed_text``);
* ``adopt_carried_state`` (the ask_user carry, encoded explicitly);
* turn-loop lifecycle integration: a production turn opens the ledger, the
  stream tap appends through it, and EVERY exit path settles it — success,
  the #756 finalize error envelope, and the ask_user early return (whose
  resume turn re-adopts the carried in-flight assistant state).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.tool_observer import _mirror_transcript_state
from clio_agent.gact.transcript import (
    TranscriptFrozenError,
    TurnTranscript,
)
from clio_agent.gact.types import Part

from .conftest import complete_turn

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake (tests that monkeypatch
# ``_try_streamed_forward`` are unaffected).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        self.events.append((event_type, dict(payload)))


def _make_transcript() -> tuple[TurnTranscript, _RecordingPublisher]:
    publisher = _RecordingPublisher()
    return (
        TurnTranscript(
            session_id="sess_t",
            turn_id="turn_t",
            publisher=publisher,
        ),
        publisher,
    )


def _tool_part(part_id: str = "") -> Part:
    return Part(
        id=part_id,
        type="tool_call",
        agent_id="data",
        call_id=f"call_{part_id or 'x'}",
        tool_name="fs_read_file",
        input={"path": "README.md"},
        metadata={"stream_source": "live"},
    )


# ---------------------------------------------------------------------------
# ensure_message guard (PR1-verdict carry-over)
# ---------------------------------------------------------------------------


def test_ensure_message_on_frozen_never_minted_ledger_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a silent empty-string message id: frozen + unminted must raise."""

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, publisher = _make_transcript()
    transcript.abandon()

    with pytest.raises(TranscriptFrozenError):
        transcript.ensure_message()

    assert publisher.events == []
    assert transcript.message_id == ""
    late = [fields for stage, fields in audits if stage == "transcript.late_op"]
    assert [f["op"] for f in late] == ["ensure_message"]


def test_ensure_message_on_frozen_minted_ledger_returns_existing_id() -> None:
    """Frozen but already minted is an honest idempotent read, not an error."""

    transcript, publisher = _make_transcript()
    minted = transcript.ensure_message()
    transcript.finalize()
    events_before = len(publisher.events)

    assert transcript.ensure_message() == minted
    assert len(publisher.events) == events_before


# ---------------------------------------------------------------------------
# abandon(): the PR2-window settle
# ---------------------------------------------------------------------------


def test_abandon_freezes_without_closing_or_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, publisher = _make_transcript()
    transcript.append_text_delta("main", "answer", "still open")
    events_before = len(publisher.events)

    transcript.abandon()
    transcript.abandon()  # idempotent

    assert transcript.frozen
    # No close, no completed event, no publish of any kind.
    assert len(publisher.events) == events_before
    assert transcript.snapshot()[0].text == ""  # open part left as-is
    # Late producer ops are rejected + audited, never absorbed.
    transcript.append_text_delta("main", "answer", "late chunk")
    assert transcript.append_part(_tool_part("p_late")) is None
    assert len(publisher.events) == events_before
    late_ops = [f["op"] for stage, f in audits if stage == "transcript.late_op"]
    assert late_ops == ["append_text_delta", "append_part"]


# ---------------------------------------------------------------------------
# legacy-equivalent state queries (what finalize now reads)
# ---------------------------------------------------------------------------


def test_current_stream_part_id_mirrors_legacy_closure_var_semantics() -> None:
    transcript, _ = _make_transcript()
    assert transcript.current_stream_part_id is None

    transcript.append_text_delta("main", "reasoning", "thinking ")
    first_id = transcript.current_stream_part_id
    assert first_id is not None

    # Close via (agent, field) change: the id moves to the NEW part (legacy:
    # immediately replaced), it does not go None.
    transcript.append_text_delta("main", "answer", "answering")
    second_id = transcript.current_stream_part_id
    assert second_id is not None
    assert second_id != first_id

    # An explicit close does NOT reset it (legacy ``_close_streamed_part``
    # never reset the closure var) — finalize's live-vs-batch provenance
    # depends on exactly that.
    transcript.close_open_text()
    assert transcript.current_stream_part_id == second_id

    # An atomic append IS the runtime boundary: it resets the id (legacy:
    # the boundary hook).
    transcript.append_part(_tool_part("p1"))
    assert transcript.current_stream_part_id is None


def test_was_closed_live_covers_closed_and_dropped_parts() -> None:
    transcript, _ = _make_transcript()
    transcript.append_text_delta("main", "reasoning", "kept text")
    kept_id = transcript.current_stream_part_id
    assert kept_id is not None
    assert not transcript.was_closed_live(kept_id)
    transcript.close_open_text()
    assert transcript.was_closed_live(kept_id)

    # Whitespace-only part: dropped at close, still marked closed-live so the
    # finalize publish loop never re-publishes the stale snapshot copy.
    transcript.append_text_delta("main", "answer", "   ")
    dropped_id = transcript.current_stream_part_id
    assert dropped_id is not None
    transcript.close_open_text()
    assert transcript.was_closed_live(dropped_id)
    assert all(p.id != dropped_id for p in transcript.snapshot())


GEOSPATIAL_NARRATION = (
    "Three ndp children are now running for Los Angeles, San Diego, and Seattle. "
    "Each runs the full discover, so I'll wait with a longer budget to collect "
    "their ranked station counts."
)


def test_same_field_restream_without_discard_duplicates_content() -> None:
    """D15 root-cause pin: TWO ``append_text_delta`` calls for the SAME still-open
    ``(agent_id, field)`` concatenate into ONE part -- correct for a legitimate
    same-field continuation (more tokens of the SAME answer), but exactly the
    mechanism that produced the live duplicate (sess_539d24da07bf part
    part_2b645566433b: one 224-char next_thought paragraph, twice, 472 chars
    total). Characterizes the vulnerability :meth:`TurnTranscript.discard_open_text`
    exists to close off at the LM transient-retry boundary."""

    transcript, _ = _make_transcript()
    transcript.append_text_delta("main", "next_thought", GEOSPATIAL_NARRATION)
    # An abandoned attempt's retry re-streams the SAME text from scratch, through
    # a brand-new field extractor with no memory of the first attempt -- and lands
    # on the SAME still-open part because (agent_id, field) hasn't changed.
    transcript.append_text_delta("main", "next_thought", GEOSPATIAL_NARRATION)
    transcript.close_open_text()

    parts = [p for p in transcript.snapshot() if p.type == "text"]
    assert len(parts) == 1
    assert parts[0].text == GEOSPATIAL_NARRATION + GEOSPATIAL_NARRATION


def test_discard_open_text_prevents_retry_duplication() -> None:
    """D15 fix: calling ``discard_open_text`` at the retry boundary (what
    ``lm_activity.note_lm_retry_reset`` does, from
    ``lm.io_logging.IOLoggingLM.__call__``'s transient-retry loop) abandons the
    failed attempt's contribution BEFORE the retry streams, so the retry's fresh
    text is the part's ONLY content -- the fix for the duplication characterized
    above."""

    transcript, publisher = _make_transcript()
    transcript.append_text_delta("main", "next_thought", GEOSPATIAL_NARRATION)
    abandoned_id = transcript.current_stream_part_id
    assert abandoned_id is not None

    discarded = transcript.discard_open_text()
    assert discarded is True
    # Never published as closed/completed -- it never counted.
    assert not any(evt == "message.part.completed" for evt, _ in publisher.events)
    # Idempotent: nothing open the second time.
    assert transcript.discard_open_text() is False

    transcript.append_text_delta("main", "next_thought", GEOSPATIAL_NARRATION)
    retry_id = transcript.current_stream_part_id
    assert retry_id is not None
    assert retry_id != abandoned_id  # the retry opens a genuinely fresh part
    transcript.close_open_text()

    parts = [p for p in transcript.snapshot() if p.type == "text"]
    assert len(parts) == 1
    assert parts[0].text == GEOSPATIAL_NARRATION  # exactly once, not doubled
    assert all(p.id != abandoned_id for p in transcript.snapshot())


def test_discard_open_text_is_noop_when_nothing_open() -> None:
    """The common case: most transient failures happen before any field starts
    streaming, so there is nothing to discard -- must be a safe, cheap no-op."""

    transcript, publisher = _make_transcript()
    assert transcript.discard_open_text() is False
    assert publisher.events == []


def test_discard_open_text_on_frozen_ledger_is_audited_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry landing after the turn already settled must not silently corrupt
    the frozen ledger -- audited as a late op, same discipline as every other
    post-freeze mutation this module rejects (mirrors
    ``test_abandon_freezes_without_closing_or_publishing``'s late-op assertion)."""

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, _ = _make_transcript()
    transcript.append_text_delta("main", "next_thought", GEOSPATIAL_NARRATION)
    transcript.abandon()

    assert transcript.discard_open_text() is False
    late_ops = [f["op"] for stage, f in audits if stage == "transcript.late_op"]
    assert late_ops == ["discard_open_text"]


def test_raw_streamed_text_concatenates_across_agents_fields_and_thinking() -> None:
    """The whole-turn concat the timeout/streaming-failure partials report —
    byte-identical to the legacy ``streamed_assistant_buffer`` join."""

    transcript, _ = _make_transcript()
    assert transcript.raw_streamed_text() == ""
    transcript.append_text_delta("main", "provider_thinking:anthropic", "mull ")
    transcript.append_text_delta("child", "reasoning", "think ")
    transcript.append_part(_tool_part("p1"))
    transcript.append_text_delta("child", "answer", "answer")
    assert transcript.raw_streamed_text() == "mull think answer"


# ---------------------------------------------------------------------------
# adopt_carried_state (the ask_user carry)
# ---------------------------------------------------------------------------


def test_adopt_carried_state_seeds_identity_without_publishing() -> None:
    transcript, publisher = _make_transcript()
    carried = [_tool_part("p_carried")]
    transcript.adopt_carried_state(
        "msg_asst_carried",
        parts=carried,
        once_keys={"route:data"},
    )

    assert transcript.message_id == "msg_asst_carried"
    assert [p.id for p in transcript.snapshot()] == ["p_carried"]
    assert transcript.has_part_key("route:data")
    assert publisher.events == []  # nothing re-published

    # The carried message id is never re-minted...
    assert transcript.ensure_message() == "msg_asst_carried"
    assert publisher.events == []
    # ...and a carried once-key stays consumed.
    assert transcript.append_part_once("route:data", _tool_part("p_dup")) is None


def test_adopt_carried_state_requires_a_fresh_ledger() -> None:
    minted, _ = _make_transcript()
    minted.ensure_message()
    with pytest.raises(RuntimeError):
        minted.adopt_carried_state("msg_x", parts=[], once_keys=set())

    frozen, _ = _make_transcript()
    frozen.abandon()
    with pytest.raises(RuntimeError):
        frozen.adopt_carried_state("msg_x", parts=[], once_keys=set())


# ---------------------------------------------------------------------------
# turn-loop lifecycle integration
# ---------------------------------------------------------------------------


class _Pred:
    def __init__(self, **fields: Any) -> None:
        self.answer = ""
        self.selected_expert = ""
        self.routing_rationale = ""
        for key, value in fields.items():
            setattr(self, key, value)


class _RegistrySpyAgent:
    """Asserts (from inside forward) that the turn opened a ledger."""

    def __init__(self, app_ref: dict[str, Any]) -> None:
        self._app_ref = app_ref
        self.saw_open_transcript: list[bool] = []

    def forward(self, question: str, session_id: str) -> Any:
        app = self._app_ref["app"]
        transcript = app.state.turn_transcripts.get(session_id)
        self.saw_open_transcript.append(transcript is not None)
        return _Pred(answer="spy answer", selected_expert="main")


def _build(tmp_path: Path, name: str, agent: Any) -> Any:
    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.gact.app import build_app

    return build_app(
        sessions_path=tmp_path / f"{name}.json",
        agent=agent,
        arc=ARCMemory(data_dir=str(tmp_path / f"arc_{name}"), store=_MemoryStore()),
    )


def test_turn_loop_opens_ledger_during_turn_and_closes_on_success(tmp_path: Path) -> None:
    app_ref: dict[str, Any] = {}
    agent = _RegistrySpyAgent(app_ref)
    app = _build(tmp_path, "lifecycle", agent)
    app_ref["app"] = app
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        complete_turn(client, sid, "hello")

        assert agent.saw_open_transcript == [True]
        # Settled at turn end: closed in the registry, legacy dicts cleaned.
        assert app.state.turn_transcripts.get(sid) is None
        assert sid not in getattr(app.state, "live_assistant_parts", {})
        assert sid not in getattr(app.state, "live_assistant_message_ids", {})


def test_stream_tap_appends_through_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streamed answer's live part is authored by the transcript: same
    ledger object serves the live alias, and the persisted part reuses the
    streamed part id (fold/reload identity)."""

    seen: dict[str, Any] = {}

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("streamed ")
        transcript = app.state.turn_transcripts.get(sid)
        assert transcript is not None
        seen["mid_turn_alias_is_ledger"] = (
            app.state.live_assistant_parts[sid] is transcript.live_parts_alias()
        )
        seen["message_id"] = transcript.message_id
        seen["open_part_id"] = transcript.current_stream_part_id
        await emit_chunk("answer")
        return _Pred(answer="streamed answer", selected_expert="main")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "tap", _Pred)  # agent unused: streamed path intercepts
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        assistant = complete_turn(client, sid, "stream please")

        assert seen["mid_turn_alias_is_ledger"] is True
        assert assistant["id"] == seen["message_id"]
        text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
        assert [p["text"] for p in text_parts] == ["streamed answer"]
        assert text_parts[0]["id"] == seen["open_part_id"]
        assert app.state.turn_transcripts.get(sid) is None


def test_failed_finalize_still_settles_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#756 x #767: the error envelope must freeze + retire the ledger."""

    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    transcripts: dict[str, Any] = {}

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("partial ")
        transcripts["turn"] = app.state.turn_transcripts.get(sid)
        return _Pred(answer="partial answer", selected_expert="main")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "envelope", _Pred)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        ack = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text
        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = client.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)

        assert status == "error"
        # The ledger is settled: frozen (late ops rejected) and retired.
        assert app.state.turn_transcripts.get(sid) is None
        assert transcripts["turn"] is not None
        assert transcripts["turn"].frozen
        # The next turn opens a FRESH ledger without a leak eviction.
        monkeypatch.setattr(
            "clio_agent.gact.app._enrich_cancellation_error_info",
            lambda app, sid, error_info: error_info,
        )

        async def clean_forward(
            app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
        ) -> Any:
            return _Pred(answer="recovered", selected_expert="main")

        monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", clean_forward)
        assistant = complete_turn(client, sid, "again")
        assert assistant["stop_reason"] == "end_turn"
        assert app.state.turn_transcripts.get(sid) is None


def test_late_chunk_after_settle_is_rejected_and_never_repopulates_legacy_dicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executor tap can outlive the turn. A late chunk must be rejected +
    audited by the frozen ledger AND must not re-populate the popped legacy
    dicts — otherwise the dead turn's identity would be handed to the next
    turn's carried-state adoption (the poison class the settle prevents)."""

    import asyncio

    taps: dict[str, Any] = {}

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        taps["emit"] = emit_chunk
        await emit_chunk("live ")
        return _Pred(answer="live answer", selected_expert="main")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    app = _build(tmp_path, "latechunk", _Pred)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        complete_turn(client, sid, "go")
        assert sid not in getattr(app.state, "live_assistant_parts", {})

        asyncio.run(taps["emit"]("late chunk"))

        assert sid not in getattr(app.state, "live_assistant_parts", {})
        assert sid not in getattr(app.state, "live_assistant_message_ids", {})
        late = [fields for stage, fields in audits if stage == "transcript.late_op"]
        assert any(fields.get("op") == "append_text_delta" for fields in late)


class _AskUserThenAnswerAgent:
    """First forward: appends a live tool part, then asks the user.
    Second forward (the resume): answers."""

    def __init__(self) -> None:
        self.calls = 0

    def forward(self, question: str, session_id: str) -> Any:
        from clio_agent.tools.execution import current_tool_runtime

        self.calls += 1
        if self.calls == 1:
            observer = current_tool_runtime().tool_observer
            assert observer is not None
            observer("fs_read_file", {"path": "data.csv"}, "started", None)
            observer(
                "fs_read_file",
                {"path": "data.csv"},
                "completed",
                None,
                result={"ok": True, "text": "rows"},
            )
            return _Pred(
                answer="",
                selected_expert="main",
                ask_user={
                    "action": "ask_user",
                    "question": "Which column?",
                    "allow_freeform": True,
                    "reason": "ambiguous_column",
                },
            )
        return _Pred(answer=f"resumed: {question[-20:]}", selected_expert="main")


def test_ask_user_early_return_settles_ledger_and_resume_adopts_carry(
    tmp_path: Path,
) -> None:
    """The ask_user pause retires the turn's ledger (no poison) while the
    resume turn adopts the carried in-flight assistant message: same message
    id, ONE message.created, and the pre-question tool parts persist in the
    final assistant message."""

    agent = _AskUserThenAnswerAgent()
    app = _build(tmp_path, "askuser", agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        ack = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "inspect data"}]},
        )
        assert ack.status_code == 200, ack.text
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if client.get(f"/v1/sessions/{sid}").json()["status"] == "waiting_user":
                break
            time.sleep(0.05)
        session = client.get(f"/v1/sessions/{sid}").json()
        assert session["status"] == "waiting_user"

        # Ledger settled at the early return; carried state stays in the
        # legacy dicts for the resume turn to adopt.
        assert app.state.turn_transcripts.get(sid) is None
        carried_msg_id = app.state.live_assistant_message_ids[sid]
        carried_part_ids = [p.id for p in app.state.live_assistant_parts[sid]]
        assert any(pid.endswith("_call") for pid in carried_part_ids)

        question_id = session["metadata"]["pending_user_question_id"]
        answered = client.post(
            f"/v1/sessions/{sid}/questions/{question_id}/answer",
            json={"answer": "use column value"},
        )
        assert answered.status_code == 200, answered.text

        deadline = time.monotonic() + 5.0
        assistant = None
        while time.monotonic() < deadline:
            msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            settled = [
                m
                for m in msgs
                if m["role"] == "assistant" and not m.get("metadata", {}).get("live")
            ]
            if settled:
                assistant = settled[-1]
                break
            time.sleep(0.05)
        assert assistant is not None, "resume turn did not settle"

        # The resumed turn CONTINUED the carried assistant message.
        assert assistant["id"] == carried_msg_id
        persisted_ids = [p["id"] for p in assistant["parts"]]
        for pid in carried_part_ids:
            assert pid in persisted_ids
        created = [
            ev
            for ev in app.state.bus._history.get(sid, [])
            if ev.type == "message.created" and ev.payload.get("id") == carried_msg_id
        ]
        assert len(created) == 1, "the carried message id must be created exactly once"
        assert app.state.turn_transcripts.get(sid) is None


def test_mirror_of_frozen_transcript_never_touches_legacy_dicts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A settled ledger must never re-enter ``app.state`` via any shim's mirror.

    Covers the ``registry.get()`` -> ``abandon()`` race window for
    executor-thread producers: the frozen ledger already rejects the op at the
    ledger API, and the mirror must not hand the finished turn's identity and
    parts back to the just-popped legacy dicts (where the next turn's
    carried-state adoption would pick them up).
    """

    transcript, _publisher = _make_transcript()
    transcript.ensure_message()
    transcript.abandon()
    assert transcript.frozen

    app = SimpleNamespace(state=SimpleNamespace())
    with caplog.at_level(logging.WARNING):
        _mirror_transcript_state(app, "sess_t", transcript)

    assert getattr(app.state, "live_assistant_message_ids", None) in (None, {})
    assert getattr(app.state, "live_assistant_parts", None) in (None, {})
    assert any("frozen_transcript_mirror" in record.message for record in caplog.records)
