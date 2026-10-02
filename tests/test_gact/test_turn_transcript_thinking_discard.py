"""Regression tests for iowarp/gact-tui#362 (server half): the thinking-discard
trap.

``_close_open_text_locked`` must never make a streamed ``thinking`` part vanish
without a trace loud enough to notice:

(a) ``_close_open_text_locked`` drops any whitespace-only part after
    buffering (#881 — the only close-time transform). For an ordinary
    ``text`` part that is routine (a trailing blank line); for a
    ``thinking`` part it means the reasoning stream opened a part and then
    never actually delivered content for it — never benign — so the drop
    escalates to ``logger.warning`` while everything else stays at INFO. The
    ``transcript.dropped_empty_part`` stream_audit row (unchanged) already
    carried ``part_type``.

Sabotage notes accompany each key assertion.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.transcript import TurnTranscript


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
