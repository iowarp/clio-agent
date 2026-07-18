"""Tests for the finalize-time stream-provenance metadata assembler.

``assemble_stream_metadata`` (turn_stream.py) stamps ``stream_source`` (live vs
batch) onto the assistant message metadata and, on a batch answer, records the
delivery-path ``stream_fallback`` payload. It was formerly
``assemble_stream_and_degradation_metadata`` in the retired ``turn_degradation``
module; the turn-degradation ledger it also drained was deleted in #948 S4 when
its sole reason -- an answer substituted from settle evidence -- was removed with
the settle/synthesis orchestration layer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.turn_stream import assemble_stream_metadata


def _app() -> Any:
    """A minimal app carrying a mutable ``.state``."""

    return SimpleNamespace(state=SimpleNamespace())


def _finalize_state(app: Any, sid: str, **overrides: Any) -> SimpleNamespace:
    """A finalize-shaped state stub (only the fields the assembler reads)."""

    base: dict[str, Any] = {
        "app": app,
        "sid": sid,
        "answer_text": "the answer",
        "error_info": None,
        "assistant_metadata": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_assembler_preserves_stream_provenance_batch() -> None:
    """A batch answer with a real stream_fallback stamps ``stream_source='batch'``
    and the fallback payload; no degradation key is ever added."""

    app = _app()
    state = _finalize_state(app, "sess_sp")
    fallback = {"reason": "sync_execution_path"}
    assemble_stream_metadata(
        state, stream_fallback=fallback, current_stream_part_id=None, has_live_parts=False
    )
    assert state.assistant_metadata["stream_source"] == "batch"
    assert state.assistant_metadata["stream_fallback"] == fallback
    assert "turn_degradations" not in state.assistant_metadata


def test_assembler_marks_live_when_streamed() -> None:
    """A live-streamed answer stamps ``stream_source='live'`` and NO stream_fallback."""

    app = _app()
    state = _finalize_state(app, "sess_live")
    assemble_stream_metadata(
        state, stream_fallback={}, current_stream_part_id="p1", has_live_parts=True
    )
    assert state.assistant_metadata["stream_source"] == "live"
    assert "stream_fallback" not in state.assistant_metadata


def test_assembler_marks_batch_error_only_turn() -> None:
    """An error-only turn (no answer text) still reports provenance: a batch error
    stamps ``stream_source='batch'`` and records the delivery-path fallback."""

    app = _app()
    state = _finalize_state(
        app, "sess_err", answer_text="", error_info=SimpleNamespace(error="boom")
    )
    fallback = {"reason": "sync_execution_path"}
    assemble_stream_metadata(
        state, stream_fallback=fallback, current_stream_part_id=None, has_live_parts=False
    )
    assert state.assistant_metadata["stream_source"] == "batch"
    assert state.assistant_metadata["stream_fallback"] == fallback
