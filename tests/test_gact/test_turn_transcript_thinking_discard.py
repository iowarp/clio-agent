"""Regression tests for iowarp/gact-tui#362 (server half): the thinking-discard
trap.

Two paths in ``TurnTranscript`` can make a streamed ``thinking`` part vanish
without a trace loud enough to notice:

(a) ``_close_open_text_locked`` drops any whitespace-only part after
    buffering (#881 — the only close-time transform). For an ordinary
    ``text`` part that is routine (a trailing blank line); for a
    ``thinking`` part it means the reasoning stream opened a part and then
    never actually delivered content for it — never benign — so the drop
    escalates to ``logger.warning`` while everything else stays at INFO. The
    ``transcript.dropped_empty_part`` stream_audit row (unchanged) already
    carried ``part_type``.

(b) ``discard_open_text`` (the LM transient-retry boundary, D15) removes an
    open part WITHOUT ever publishing it — this already emitted
    ``transcript.discarded_retry_part`` with ``part_type``/``part_id``/
    ``chunk_len`` before this issue; pinned here as a regression lock so a
    future refactor cannot silently drop it.

Sabotage notes accompany each key assertion.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.transcript import TurnTranscript
from clio_agent.gact.types import Part


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        self.events.append((event_type, dict(payload)))

    def of_type(self, *event_types: str) -> list[tuple[str, dict[str, Any]]]:
        return [(t, p) for (t, p) in self.events if t in event_types]


def _make_transcript() -> tuple[TurnTranscript, RecordingPublisher]:
    publisher = RecordingPublisher()
    transcript = TurnTranscript(session_id="sess_thk", turn_id="turn_thk", publisher=publisher)
    return transcript, publisher


# ---------------------------------------------------------------------------
# (a) whitespace-only THINKING drop escalates to WARNING; text stays INFO.
# ---------------------------------------------------------------------------


def test_whitespace_only_thinking_drop_logs_warning(caplog: Any) -> None:
    transcript, publisher = _make_transcript()
    with caplog.at_level(logging.INFO, logger="clio_agent.gact.transcript"):
        transcript.append_text_delta("main", "provider_thinking:openai", "   \n")
        transcript.append_text_delta("main", "provider_thinking:openai", "  \t")
        transcript.close_open_text()

    # Same drop behavior as before: removed from the ledger, nothing published.
    assert transcript.snapshot() == []
    assert publisher.of_type("message.part.completed") == []

    dropped = [r for r in caplog.records if "dropped_empty_part" in r.getMessage()]
    assert len(dropped) == 1
    # Sabotage: keep logger.info for every part type (ignore part.type == "thinking")
    # -> this goes red (the whole point of the fix: a whitespace-only thinking part
    # is never benign, so it must be loud, not buried at INFO).
    assert dropped[0].levelno == logging.WARNING
    assert "type=thinking" in dropped[0].getMessage()


def test_whitespace_only_text_drop_stays_info(caplog: Any) -> None:
    """Regression pin: the escalation is SCOPED to ``thinking`` — an ordinary
    blank ``text`` part (e.g. a trailing newline the model emits after its
    real answer) must not start spamming WARNING."""

    transcript, publisher = _make_transcript()
    with caplog.at_level(logging.INFO, logger="clio_agent.gact.transcript"):
        transcript.append_text_delta("main", "answer", "   \n")
        transcript.close_open_text()

    assert transcript.snapshot() == []
    dropped = [r for r in caplog.records if "dropped_empty_part" in r.getMessage()]
    assert len(dropped) == 1
    # Sabotage: escalate every part type to WARNING -> this goes red.
    assert dropped[0].levelno == logging.INFO
    assert "type=text" in dropped[0].getMessage()


def test_whitespace_only_thinking_drop_audit_row_carries_part_type(
    monkeypatch: Any,
) -> None:
    """The stream_audit row already carried part_type; ``chars`` (the
    review's N2 finding) was added so a whitespace-only drop's size is
    visible without cross-referencing the log line's raw text."""

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, _publisher = _make_transcript()
    transcript.append_text_delta("main", "provider_thinking:anthropic", "   ")
    transcript.close_open_text()

    rows = [f for stage, f in audits if stage == "transcript.dropped_empty_part"]
    assert len(rows) == 1
    # Sabotage: drop part_type from the stream_audit call -> KeyError / red.
    assert rows[0]["part_type"] == "thinking"
    assert rows[0]["session_id"] == "sess_thk"
    assert rows[0]["turn_id"] == "turn_thk"
    # Sabotage: drop the chars= kwarg -> KeyError / red (N2).
    assert rows[0]["chars"] == len("   ")


# ---------------------------------------------------------------------------
# (b) discard_open_text -- already emits transcript.discarded_retry_part
# keyed by part type; pinned as a regression lock (gact-tui#362 asked for
# this and it was already true -- kept as the regression pin).
# ---------------------------------------------------------------------------


def test_discard_open_text_audits_by_part_type_for_thinking(monkeypatch: Any) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, publisher = _make_transcript()
    transcript.append_text_delta("main", "provider_thinking:openai", "half a thought")
    part_id = transcript.current_stream_part_id
    assert part_id is not None

    discarded = transcript.discard_open_text()
    assert discarded is True
    # Never published as completed -- discard, not close (it never counted).
    assert publisher.of_type("message.part.completed") == []

    rows = [f for stage, f in audits if stage == "transcript.discarded_retry_part"]
    # Sabotage: stop calling stream_audit in discard_open_text -> this list is
    # empty -> red (the exact regression this issue guards against).
    assert len(rows) == 1
    assert rows[0]["part_type"] == "thinking"
    assert rows[0]["part_id"] == part_id
    assert rows[0]["chunk_len"] == len("half a thought")


def test_discard_open_text_audits_by_part_type_for_text(monkeypatch: Any) -> None:
    """Same lock for the plain ``text`` case (D15's original narration example),
    keyed distinctly by ``part_type == "text"``."""

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, _publisher = _make_transcript()
    transcript.append_text_delta("main", "next_thought", "an abandoned attempt")
    assert transcript.discard_open_text() is True

    rows = [f for stage, f in audits if stage == "transcript.discarded_retry_part"]
    assert len(rows) == 1
    assert rows[0]["part_type"] == "text"
    assert rows[0]["chunk_len"] == len("an abandoned attempt")


def test_tool_part_whitespace_close_never_reaches_the_thinking_escalation() -> None:
    """Sanity: the escalation branch only ever inspects the OPEN TEXT part being
    closed -- appending an atomic tool_call part must not somehow trip it."""

    transcript, publisher = _make_transcript()
    transcript.append_text_delta("main", "provider_thinking:openai", "   ")
    appended = transcript.append_part(
        Part(id="", type="tool_call", agent_id="main", call_id="c1", tool_name="fs_read_file")
    )
    assert appended is not None
    # The whitespace-only thinking part closed (and dropped) as a side effect of
    # the tool part's boundary; the tool part itself lands normally.
    assert [p.type for p in transcript.snapshot()] == ["tool_call"]
