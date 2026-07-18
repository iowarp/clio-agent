"""Failing-first tests for the unified turn-degradation ledger (#736 unify).

#736 landed the record-half of a no-silent-fallback mechanism as two write-only
sibling ledgers that nothing in ``src/`` ever read. These tests lock the unified
mechanism: one always-on, typed-catalog-validated LIST ledger
(``app.state.turn_degradations``) DRAINED once at finalize onto the assistant
message metadata, so the degradation actually reaches the trace/API. The teeth
(test :func:`test_drain_lands_on_persisted_message_metadata`) fail if the drain
call inside :func:`assemble_stream_and_degradation_metadata` is removed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.runtime.capabilities import _stream_fallback_reason_capabilities
from clio_agent.gact.turn_degradation import (
    _TURN_DEGRADATION_REASON_DEFINITIONS,
    _turn_degradation_payload,
    assemble_stream_and_degradation_metadata,
    pop_turn_degradations,
    record_turn_degradation,
    substitute_answer_from_delegation_evidence,
)
from clio_agent.gact.types import Message


def _app() -> Any:
    """A minimal app carrying a mutable ``.state`` for the per-session ledger."""

    return SimpleNamespace(state=SimpleNamespace())


def _finalize_state(app: Any, sid: str, **overrides: Any) -> SimpleNamespace:
    """A finalize-shaped state stub (fields the assembler + substitution read)."""

    base: dict[str, Any] = {
        "app": app,
        "sid": sid,
        "answer_text": "the answer",
        "error_info": None,
        "assistant_metadata": {},
        "expert_handoffs": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _parent_resumed_row(text: str) -> dict[str, Any]:
    return {
        "agent_id": "main",
        "status": "completed",
        "stage": "parent.resumed",
        "output": text,
        "workflow_state": {},
    }


# --------------------------------------------------------------------------- #
# (a) THE teeth: the drain lands on the PERSISTED assistant message metadata   #
# --------------------------------------------------------------------------- #


def test_drain_lands_on_persisted_message_metadata() -> None:
    """Degradation records seeded on the unified ledger drain onto
    ``state.assistant_metadata`` (which finalize hands VERBATIM to the persisted
    ``Message(metadata=...)`` + ``turn.completed`` payload) and the ledger is emptied.
    Deleting the drain inside ``assemble_stream_and_degradation_metadata`` MUST make
    this fail — the ledger becomes write-only again."""

    app = _app()
    sid = "sess_a"
    record_turn_degradation(
        app, sid, "answer_substituted_from_delegation_evidence", "parent=main child=analysis"
    )
    record_turn_degradation(
        app, sid, "answer_substituted_from_delegation_evidence", "parent=main child=synthesis"
    )

    state = _finalize_state(app, sid)
    assemble_stream_and_degradation_metadata(
        state, stream_fallback={}, current_stream_part_id=None, has_live_parts=False
    )

    reasons = [d["reason"] for d in state.assistant_metadata["turn_degradations"]]
    assert reasons == [
        "answer_substituted_from_delegation_evidence",
        "answer_substituted_from_delegation_evidence",
    ]
    # The ledger is drained (destructive pop) so a later turn cannot re-emit them.
    assert pop_turn_degradations(app, sid) == []

    # Reaches the persisted assistant message EXACTLY as finalize hands it off
    # (Message(metadata=state.assistant_metadata) + completed_payload["metadata"]).
    msg = Message(
        id="m",
        session_id=sid,
        role="assistant",
        created_at="t",
        updated_at="t",
        metadata=state.assistant_metadata,
    )
    assert [d["reason"] for d in msg.metadata["turn_degradations"]] == reasons


def test_assembler_preserves_stream_provenance_batch() -> None:
    """The relocated stream-provenance block stays byte-identical: a batch answer with
    a real stream_fallback still stamps ``stream_source='batch'`` + the fallback."""

    app = _app()
    state = _finalize_state(app, "sess_sp")
    fallback = {"reason": "sync_execution_path"}
    assemble_stream_and_degradation_metadata(
        state, stream_fallback=fallback, current_stream_part_id=None, has_live_parts=False
    )
    assert state.assistant_metadata["stream_source"] == "batch"
    assert state.assistant_metadata["stream_fallback"] == fallback
    # No degradations recorded -> the key is absent (not an empty list).
    assert "turn_degradations" not in state.assistant_metadata


def test_assembler_marks_live_when_streamed() -> None:
    """A live-streamed answer stamps ``stream_source='live'`` and NO stream_fallback."""

    app = _app()
    state = _finalize_state(app, "sess_live")
    assemble_stream_and_degradation_metadata(
        state, stream_fallback={}, current_stream_part_id="p1", has_live_parts=True
    )
    assert state.assistant_metadata["stream_source"] == "live"
    assert "stream_fallback" not in state.assistant_metadata


# --------------------------------------------------------------------------- #
# (b) the substitution point records the content-swap reason                   #
# --------------------------------------------------------------------------- #


def test_substitution_point_records_reason() -> None:
    """Non-empty delegation evidence is returned AND records
    ``answer_substituted_from_delegation_evidence`` on the ledger."""

    app = _app()
    sid = "sess_sub"
    state = _finalize_state(
        app, sid, answer_text="", expert_handoffs=[_parent_resumed_row("EVIDENCE TEXT")]
    )
    result = substitute_answer_from_delegation_evidence(state)
    assert result == "EVIDENCE TEXT"
    entries = pop_turn_degradations(app, sid)
    reasons = [p["reason"] for p in entries]
    assert reasons == ["answer_substituted_from_delegation_evidence"]
    assert entries[0]["category"] == "delegation_degradation"


def test_substitution_empty_evidence_records_nothing() -> None:
    """An empty-evidence handoff list is NOT a substitution (finalize raises
    empty_response) so NOTHING is recorded."""

    app = _app()
    sid = "sess_empty"
    state = _finalize_state(app, sid, answer_text="", expert_handoffs=[_parent_resumed_row("")])
    result = substitute_answer_from_delegation_evidence(state)
    assert result == ""
    assert pop_turn_degradations(app, sid) == []


# --------------------------------------------------------------------------- #
# (d2) a record that cannot be attributed is SURFACED, never silently dropped   #
# --------------------------------------------------------------------------- #


def test_record_appless_warns_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    """A ``record_turn_degradation`` with no app CANNOT persist, but it must not
    vanish silently (the old ``bare return``). It emits a WARNING naming the reason
    so the dropped downgrade reaches the logs/trace (no-silent-fallback). Deleting
    that WARNING (reverting to the bare return) makes this fail."""

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.turn_degradation"):
        record_turn_degradation(
            None, "sess_appless", "answer_substituted_from_delegation_evidence", "p=main"
        )

    hits = [
        r
        for r in caplog.records
        if "turn degradation not persisted" in r.message
        and "answer_substituted_from_delegation_evidence" in r.message
    ]
    assert hits, "app-less record was silently dropped (no WARNING emitted)"
    assert hits[0].levelno == logging.WARNING


def test_record_stateless_app_warns_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    """An app object with NO ``.state`` (the ``per_app_dict`` throwaway-``{}`` shape the
    lead flagged) also surfaces a WARNING instead of dropping the record into a
    discarded dict."""

    stateless_app = SimpleNamespace()  # no .state attribute at all

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.turn_degradation"):
        record_turn_degradation(
            stateless_app, "sess_stateless", "answer_substituted_from_delegation_evidence", "p=main"
        )

    assert any("turn degradation not persisted" in r.message for r in caplog.records), (
        "state-less record was silently dropped (no WARNING emitted)"
    )
    # And nothing was smuggled onto the throwaway object.
    assert getattr(stateless_app, "turn_degradations", None) is None


def test_record_empty_sid_warns_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    """A live app but an EMPTY session id still cannot attribute the record, so it is
    surfaced (WARNING) rather than dropped, and the ledger is untouched."""

    app = _app()
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.turn_degradation"):
        record_turn_degradation(app, "", "answer_substituted_from_delegation_evidence", "p=main")

    assert any("turn degradation not persisted" in r.message for r in caplog.records)
    assert getattr(app.state, "turn_degradations", None) in (None, {})


def test_concurrent_sessions_never_orphan_a_ledger() -> None:
    """Many concurrent turns for DISTINCT sessions racing the ledger's first-touch on a
    SHARED app must all persist -- the lazy get-or-create is serialized so no session's
    ledger is orphaned by a colliding ``setattr`` (the silent-loss the module prevents,
    one layer down). Deterministically green with the lock; a reintroduced non-atomic
    create is a lost-update bug this guards."""

    import threading

    app = _app()
    n = 48
    ready = threading.Barrier(n)

    def _record(i: int) -> None:
        ready.wait()  # release all threads at once to maximize first-touch collision
        record_turn_degradation(
            app, f"sess_{i}", "answer_substituted_from_delegation_evidence", f"m{i}"
        )

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store = app.state.turn_degradations
    missing = [f"sess_{i}" for i in range(n) if f"sess_{i}" not in store]
    assert not missing, f"orphaned session ledgers under concurrent first-touch: {missing}"
    assert all(len(store[f"sess_{i}"]) == 1 for i in range(n))


# --------------------------------------------------------------------------- #
# (e) unknown reason rejected                                                  #
# --------------------------------------------------------------------------- #


def test_turn_degradation_payload_rejects_unknown_reason() -> None:
    """An unknown reason is rejected (no bare fallback), mirroring stream_fallback."""

    with pytest.raises(ValueError, match="Unknown turn degradation reason"):
        _turn_degradation_payload("not_a_real_reason")


# --------------------------------------------------------------------------- #
# (f) the degradation reasons never contaminate the closed streaming set        #
# --------------------------------------------------------------------------- #


def test_degradation_reasons_absent_from_streaming_capability_set() -> None:
    """None of the 3 turn-degradation reasons appear in the audited, client-facing
    stream_fallback capability set (a closed live-streaming set stays uncontaminated)."""

    streaming = _stream_fallback_reason_capabilities()
    for reason in _TURN_DEGRADATION_REASON_DEFINITIONS:
        assert reason not in streaming


