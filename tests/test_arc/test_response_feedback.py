"""Feedback is a durable ARC record family, outside the model context plane."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.response_feedback import (
    FeedbackConflict,
    ResponseFeedback,
    ResponseFeedbackLedger,
)
from clio_agent.arc.storage import ARCStore, make_arc_store


def _feedback(*, rating: str | None = "good", session_id: str = "s1") -> ResponseFeedback:
    return ResponseFeedback(
        feedback_id=uuid4(),
        rating=rating,
        session_id=session_id,
        message_id="answer",
        turn_id="question",
        response_text="A response",
        response_sha256="snapshot_hash",
        message_created_at="2026-10-10T00:00:00Z",
        stop_reason="end_turn",
    )


@pytest.mark.parametrize("backend", ["memory", "cte"])
def test_history_survives_a_new_ledger_and_arc_release(tmp_path: Path, backend: str) -> None:
    store: ARCStore = (
        _MemoryStore() if backend == "memory" else make_arc_store(namespace=f"feedback-{uuid4()}")
    )
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    first = arc.response_feedback.record(_feedback(), expected_feedback_id=None)
    bad = arc.response_feedback.record(
        _feedback(rating="bad"), expected_feedback_id=first.feedback_id
    )
    removed = arc.response_feedback.record(
        _feedback(rating=None), expected_feedback_id=bad.feedback_id
    )
    arc.release_session("s1")
    # No hot cache or UI state: a fresh ledger must read clio-core's records.
    reader = ResponseFeedbackLedger(store)
    assert reader.current("s1", "answer") == removed
    assert [row.rating for row in reader.history("s1")] == ["good", "bad", None]
    assert [row.previous_feedback_id for row in reader.history("s1")] == [
        None,
        first.feedback_id,
        bad.feedback_id,
    ]
    assert reader.history("other-session") == []
    assert arc.render_segments("s1", "main") == []
    for name, _ in list(store.scan("response_feedback")):
        store.delete("response_feedback", name)


def test_retries_do_not_duplicate_and_stale_changes_conflict() -> None:
    ledger = ResponseFeedbackLedger(_MemoryStore())
    request = _feedback()
    good = ledger.record(request, expected_feedback_id=None)
    assert ledger.record(request, expected_feedback_id=None) == good
    with pytest.raises(FeedbackConflict, match="different rating"):
        ledger.record(request.model_copy(update={"rating": "bad"}), expected_feedback_id=None)
    with pytest.raises(FeedbackConflict, match="changed elsewhere"):
        ledger.record(_feedback(rating="bad"), expected_feedback_id=None)
    assert len(ledger.history("s1")) == 1


def test_concurrent_ratings_cannot_silently_replace_each_other() -> None:
    ledger = ResponseFeedbackLedger(_MemoryStore())

    def rate(_: int) -> str:
        try:
            ledger.record(_feedback(), expected_feedback_id=None)
            return "saved"
        except FeedbackConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(rate, range(2))) == ["conflict", "saved"]
    assert len(ledger.history("s1")) == 1
