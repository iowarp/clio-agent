"""``FieldStream``: the exactly-once text channel of one ``(agent_id, field)`` (#767).

Extracted verbatim from ``gact/transcript.py`` (#1334: that module pays its size ratchet
for the seal hook of the streaming-native persistence, #1337). It reaches the
``TurnTranscript`` privates it always did (the lock, the open part, the closed-text
identity map, the batch append) — the channel IS transcript logic, split by file, not by
ownership. ``transcript.FieldStream`` stays importable (re-export).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from clio_agent.gact.transcript import TurnTranscript


class FieldStream:
    """Exactly-once text channel for one ``(agent_id, field)`` within one turn.

    Take the handle around the LM call; deltas reach the transcript either
    through :meth:`append` or directly through the stream tap
    (``append_text_delta``) — the handle seeds its identity from the turn's
    ledger state at construction, so both producer shapes count.
    :meth:`finish` settles the channel — by op identity, never by string
    comparison — deciding whether the batch ``fallback_text`` lands:

    - a non-empty part landed for the channel -> keep it (closing the open
      part first when it carries this channel's field); fallback audited + ignored
    - nothing landed + fallback               -> ONE added+completed batch burst
    - neither                                 -> ``None``

    ``covers`` widens the channel to a SET of agent labels
    (:meth:`TurnTranscript.turn_answer_stream`) — the same logical field can
    stream under more than one attribution label for one LM call.
    """

    def __init__(
        self,
        transcript: "TurnTranscript",
        agent_id: str,
        field: str,
        *,
        covers: Optional[frozenset[str]] = None,
    ) -> None:
        self._transcript = transcript
        self._agent_id = str(agent_id or "")
        self._field = str(field or "answer")
        self._covers = covers if covers is not None else frozenset({self._agent_id})
        self._finished = False
        #: The part id this handle's text landed in; ``None`` until a delta
        #: opens a part (or seeding/finish binds one).
        self.part_id: Optional[str] = None
        with transcript._lock:
            open_part = self._open_channel_part_locked()
            if open_part is not None:
                self.part_id = open_part.id

    def _open_channel_part_locked(self) -> Optional[Part]:
        """The transcript's open part when it carries THIS channel's field."""

        transcript = self._transcript
        open_part = transcript._open_part
        if open_part is None or transcript._open_field != self._field:
            return None
        if transcript._open_agent not in self._covers:
            return None
        return open_part

    def _landed_locked(self) -> bool:
        """Op identity: did this channel land a non-empty closed part this turn?"""

        closed = self._transcript._closed_text
        return any(bool(closed.get((agent, self._field))) for agent in self._covers)

    def append(self, chunk: str) -> None:
        """Route one streamed delta to the transcript; opens the part lazily."""

        if not chunk:
            return
        transcript = self._transcript
        with transcript._lock:
            if self._finished:
                transcript._audit_late_op(
                    "field_stream.append",
                    agent_id=self._agent_id,
                    field=self._field,
                )
                return
            transcript.append_text_delta(self._agent_id, self._field, chunk)
            open_part = transcript._open_part
            if open_part is not None:
                self.part_id = open_part.id

    def finish(
        self,
        *,
        fallback_text: str = "",
        fallback_metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[str]:
        """Settle the channel; returns the final text that landed, if any.

        ``fallback_metadata`` rides the batch burst's part metadata when the
        fallback lands (e.g. the turn's ``stream_fallback`` payload).
        """

        transcript = self._transcript
        with transcript._lock:
            if self._finished:
                transcript._audit_late_op(
                    "field_stream.finish",
                    agent_id=self._agent_id,
                    field=self._field,
                )
                return None
            self._finished = True
            open_part = self._open_channel_part_locked()
            if open_part is not None:
                self.part_id = open_part.id
                transcript._close_open_text_locked()
            if self._landed_locked():
                if fallback_text.strip():
                    # The batch copy of an already-landed channel is dropped by
                    # IDENTITY (a part landed this turn), never by text
                    # comparison; audited so parity data exists (#733/#736's
                    # replacement).
                    # Resolved through the transcript module at call time: tests patch
                    # ``clio_agent.gact.transcript.stream_audit`` (the pre-extraction seam).
                    from clio_agent.gact import transcript as _transcript_mod  # noqa: PLC0415

                    _transcript_mod.stream_audit(
                        "transcript.fieldstream.fallback_ignored",
                        session_id=transcript.session_id,
                        turn_id=transcript.turn_id,
                        agent_id=self._agent_id,
                        field=self._field,
                        reason="already_streamed",
                        fallback_len=len(fallback_text),
                    )
                closed = next(
                    (p for p in transcript._parts if p.id == self.part_id),
                    None,
                )
                return closed.text if closed is not None else ""
            if transcript._frozen:
                transcript._audit_late_op(
                    "field_stream.finish",
                    agent_id=self._agent_id,
                    field=self._field,
                )
                return None
            if not fallback_text.strip():
                return None
            # #881: the batch fallback is the model's answer field VERBATIM — the
            # server binds no visible-text prose cleaner, so a non-whitespace
            # fallback always lands (the whitespace-only case returned above).
            part = transcript._append_batch_text_locked(
                self._agent_id,
                self._field,
                fallback_text,
                extra_metadata=fallback_metadata,
            )
            self.part_id = part.id
            return fallback_text


__all__ = ["FieldStream"]
