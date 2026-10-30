"""Reporting for streamed parts dropped at close as whitespace-only.

A text or thinking part whose buffered text is only whitespace carries no
content, so :meth:`TurnTranscript._close_open_text_locked` removes it (#881). It
was never announced live either (gact-tui#368), so the drop is invisible on the
wire; this log line and ``stream_audit`` row are where it is recorded. Kept out of
:mod:`clio_agent.gact.transcript`, which is at its file-size ratchet.
"""

from __future__ import annotations

import logging

from clio_agent.gact.types import Part
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)


def report_dropped_empty_part(*, session_id: str, turn_id: str, part: Part, chars: int) -> None:
    """Log and audit one dropped whitespace-only part.

    A dropped thinking part logs at WARNING, a text part at INFO (gact-tui#362).

    Args:
        session_id: The session the turn belongs to.
        turn_id: The turn whose transcript dropped the part.
        part: The dropped part.
        chars: How many (whitespace) characters it had buffered.
    """

    (logger.warning if part.type == "thinking" else logger.info)(
        "turn_transcript dropped_empty_part session=%s turn=%s part=%s type=%s",
        session_id,
        turn_id,
        part.id,
        part.type,
    )
    stream_audit(
        "transcript.dropped_empty_part",
        session_id=session_id,
        turn_id=turn_id,
        part_id=part.id,
        part_type=part.type,
        chars=chars,
    )
