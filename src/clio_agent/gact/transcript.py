"""TurnTranscript — the single-writer part ledger for one assistant turn (#767, PR1+PR2).

Implements ``docs/design/turn-transcript.md``: the live SSE stream and the
persisted assistant message become two projections of ONE append-only ledger.
Every part is appended exactly once, by its producer, at the moment it
happens; the append itself publishes the wire event; ``finalize()`` closes
open parts, stamps sequence, and returns the ledger verbatim — no rewriting,
no dedup, no re-publish.

PR1 scope (design §6): this module + the tool-observer/delegation shims in
:mod:`clio_agent.gact.tool_observer`.

PR2 scope (design §6): the turn loop owns the registry lifecycle — it opens
the ledger at turn start, the stream tap (``turn.py``'s ``_emit_chunk``, now a
thin adapter) appends through :meth:`TurnTranscript.append_text_delta`, and
the loop settles the ledger on EVERY exit path (success, the #756 finalize
error envelope, and the ask_user early return). During the PR2 window the
finalize region is still the legacy reader — it re-derives its decisions from
transcript state queries (:attr:`current_stream_part_id`,
:meth:`was_closed_live`, :meth:`raw_streamed_text`, :meth:`open_text_part`)
instead of the deleted ``turn.py`` closure variables — so turn end uses
:meth:`abandon` (freeze without publishing) rather than :meth:`finalize`;
PR3 flips finalize into a pure reader of :meth:`finalize`.

THREADING CONTRACT (PR2)
- The stream tap always enters the ledger ON THE TURN'S EVENT LOOP THREAD:
  LM taps running in executor threads bridge through
  ``clio_agent.runtime.lm_activity``'s ``asyncio.run_coroutine_threadsafe``
  into the turn's async ``_emit_chunk`` adapter — the bridge is explicit,
  never a direct cross-thread ledger call.
- The tool observer and the delegation settle paths append from EXECUTOR
  THREADS (MCPToolBridge worker threads / ``run_in_executor``).
- One re-entrant lock serializes all of it; events publish while the lock is
  held, so ledger order == bus order. This requires ``bus.publish`` to be
  non-blocking (it is: ``queue.put_nowait`` behind ``call_soon_threadsafe``;
  asserted by test). Lock ordering: transcript -> bus, never the reverse.

Accretion rule: all new transcript logic lives HERE; ``turn.py`` only shrinks.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Optional, Protocol

from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.runtime.globals import (
    _iso_from_epoch,
    _new_message_id,
    _new_part_id,
)
from clio_agent.gact.types import Message, Part
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

_PROVIDER_THINKING_PREFIX = "provider_thinking:"

# Part types whose open->delta->close lifecycle the transcript owns. Everything
# else (tool_call/tool_result/expert_handoff/routing_decision/file_diff) is an
# atomic append.
_STREAMED_TEXT_TYPES = {"text", "thinking"}


def _transcript_text_field(field_name: str) -> str:
    """Map DSPy contract fields to public transcript text fields."""

    return "answer" if field_name == "answer" else "thought"


class TranscriptFrozenError(RuntimeError):
    """A producer asked a FROZEN, never-minted ledger to mint the message id.

    Minting after :meth:`TurnTranscript.finalize` / :meth:`TurnTranscript.abandon`
    would attach a fresh assistant identity to a settled turn; returning an
    empty-string id instead would be a silent fallback. The caller gets a
    structured error and the op is audited (``transcript.late_op``).
    """


class TranscriptPublisher(Protocol):
    """Injected event sink for ledger transitions.

    The default implementation wraps the app :class:`EventBus` and emits BOTH
    vocabularies (legacy ``message.part.*`` plus the normalized ``turn.text.delta``
    / ``turn.trace.delta`` twins) from ONE transition, so gact-tui#232's
    "decide the normalized channel" choice becomes a publisher config flag
    (flipped in PR5), not a code hunt.
    """

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Publish one wire event."""
        ...


class EventBusTranscriptPublisher:
    """Default :class:`TranscriptPublisher`: one ledger transition -> EventBus.

    Requires ``bus.publish`` to be non-blocking (it is: ``queue.put_nowait``
    behind ``call_soon_threadsafe``; asserted by test) because the transcript
    publishes while holding its ledger lock. Lock ordering: transcript -> bus,
    never the reverse.
    """

    def __init__(self, bus: EventBus, session_id: str) -> None:
        self._bus = bus
        self._session_id = session_id

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Publish one wire event onto the session's EventBus stream."""

        self._bus.publish(
            Event(type=event_type, session_id=self._session_id, payload=dict(payload))
        )


class TurnTranscript:
    """Single-writer part ledger for one assistant turn.

    OWNERSHIP (design §3.1)

    - Sole minter of the assistant message id and every part id for the turn.
    - Sole publisher of ``message.created`` / ``message.part.added`` / ``.delta``
      / ``.completed`` for parts it owns. (``tool.call.*``, semantic events, and
      delegation lifecycle telemetry stay with their producers — telemetry is
      not transcript.)
    - Append-only; arrival order IS persisted order; a 1-based sequence is
      stamped at :meth:`finalize` (stamping it on the ``part.added`` wire event
      would change today's byte-for-byte event shapes, which PR1 must preserve).
    - Text mutates exactly once: ``clean_text`` runs on the WHOLE buffer at
      close (text parts only; provider thinking is verbatim); the cleaned
      result is recorded into the close event AND the part. ``finalize()``
      never rewrites text.
    - Thread-safe: one re-entrant lock guards the ledger; events publish while
      holding it, so ledger order == bus order.
    - Appends after :meth:`finalize` are rejected and audited
      (``stream_audit('transcript.late_op')``), never silently absorbed into
      the next turn.
    """

    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        publisher: TranscriptPublisher,
        clean_text: Callable[[str], str],
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.message_id: str = ""
        self._publisher = publisher
        self._clean_text = clean_text
        # Re-entrant: append_part -> ensure_message / close_open_text nest.
        self._lock = threading.RLock()
        # THE ledger. This exact list object is also exposed through
        # :meth:`live_parts_alias` as ``app.state.live_assistant_parts[sid]``
        # during the PR1/PR2 migration window, so it must only ever be mutated
        # in place (never rebound) and closed text parts must mutate
        # ``Part.text`` in place (design §9 alias-view aliasing).
        self._parts: list[Part] = []
        self._buffers: dict[str, list[str]] = {}
        self._open_part: Optional[Part] = None
        self._open_agent: str = ""
        self._open_field: str = ""
        self._once_keys: set[str] = set()
        # (agent_id, field) -> closed non-empty final texts, for the identity
        # checks that replace finalize's content comparisons (design §3.1).
        self._closed_text: dict[tuple[str, str], list[str]] = {}
        # (agent_id, field) -> raw streamed chunks THIS turn (turn-scoped by
        # construction — subsumes app.state.live_streamed_field_text, #757).
        self._streamed: dict[tuple[str, str], list[str]] = {}
        # Every accepted streamed chunk in arrival order, across agents AND
        # fields (provider thinking included) — the whole-turn concat the
        # timeout/StreamingOutputError partials read; byte-identical to the
        # legacy ``streamed_assistant_buffer`` (design §9 error/cancel partials).
        self._raw_stream: list[str] = []
        # Mirrors the legacy ``streamed_assistant_part_id`` closure var: the id
        # of the last text/thinking part OPENED since the last atomic-part
        # runtime boundary. Set on open, cleared by :meth:`append_part`, NOT
        # cleared by close (legacy ``_close_streamed_part`` never reset it) —
        # finalize's live-vs-batch stream provenance depends on exactly that.
        self._current_stream_part_id: Optional[str] = None
        # Ids of streamed text/thinking parts already closed live THIS turn
        # (empty-after-clean drops included) — replaces the legacy
        # ``closed_streamed_part_ids`` closure set the finalize publish loop
        # consults. NOT seeded by :meth:`adopt_carried_state` (the legacy set
        # was per-invocation, so carried parts were never in it).
        self._closed_live_part_ids: set[str] = set()
        self._frozen = False

    # -- identity ------------------------------------------------------------

    def ensure_message(self) -> str:
        """Mint the assistant message id + publish ``message.created`` ONCE.

        Whichever producer arrives first triggers the mint; every later call
        returns the same id without re-publishing.

        Raises:
            TranscriptFrozenError: when the ledger is frozen and no message id
                was ever minted — minting now would attach a fresh identity to
                a settled turn, and returning ``""`` would be a silent
                fallback. The op is audited as ``transcript.late_op`` first.
        """

        with self._lock:
            if self.message_id:
                return self.message_id
            if self._frozen:
                self._audit_late_op("ensure_message")
                raise TranscriptFrozenError(
                    "ensure_message on a frozen, never-minted TurnTranscript "
                    f"(session={self.session_id!r} turn={self.turn_id!r})"
                )
            self.message_id = _new_message_id("asst")
            now = _iso_from_epoch(time.time())
            self._publisher.publish(
                "message.created",
                Message(
                    id=self.message_id,
                    turn_id=self.turn_id,
                    session_id=self.session_id,
                    role="assistant",
                    created_at=now,
                    updated_at=now,
                    parts=[],
                ).to_wire(),
            )
            return self.message_id

    def adopt_carried_state(
        self,
        message_id: str,
        *,
        parts: list[Part],
        once_keys: set[str],
    ) -> None:
        """Adopt an ask_user-paused turn's in-flight assistant state (PR2).

        Today an ask_user pause deliberately CARRIES the in-flight assistant
        message across the question: the resume turn adopts the same message
        id (no second ``message.created``) and its finalize persists the
        pre-question live parts into the same assistant message. This method
        encodes that carry explicitly for a FRESH ledger: set the already-
        published message id without re-publishing, seed the ledger with the
        carried parts without re-publishing their ``part.added`` events, and
        carry the once-key set so carried route banners stay once-per-message.

        This is a deliberate state transfer between two ledgers of the SAME
        session across a user question — distinct from the leaked-ledger
        poison class, which :meth:`TurnTranscriptRegistry.open_turn` still
        evicts loudly.

        Raises:
            RuntimeError: when called on a non-fresh ledger (already minted,
                already holding parts, or frozen) — adoption is only defined
                at turn open.
        """

        with self._lock:
            if self._frozen or self.message_id or self._parts or self._once_keys:
                raise RuntimeError(
                    "adopt_carried_state requires a fresh TurnTranscript "
                    f"(session={self.session_id!r} turn={self.turn_id!r})"
                )
            self.message_id = str(message_id or "")
            self._parts.extend(parts)
            self._once_keys.update(once_keys)
            logger.info(
                "turn_transcript adopted carried ask_user state session=%s turn=%s "
                "message=%s parts=%d once_keys=%d",
                self.session_id,
                self.turn_id,
                self.message_id,
                len(parts),
                len(once_keys),
            )

    # -- the ONE producer API --------------------------------------------------

    def append_part(self, part: Part, *, stream_source: str = "live") -> Optional[Part]:
        """Append an atomic non-text part and publish ``message.part.added``.

        Closes any open streamed text part first (the runtime boundary),
        assigns an id when the caller left it empty, and publishes the added
        event in one locked burst. Returns the appended part, or ``None`` when
        the ledger is already frozen (audited, never silent).
        """

        with self._lock:
            if self._frozen:
                self._audit_late_op("append_part", part_id=part.id, part_type=part.type)
                return None
            self._close_open_text_locked()
            # The atomic append IS the runtime boundary (legacy: the
            # cross-module boundary hook reset ``streamed_assistant_part_id``).
            self._current_stream_part_id = None
            msg_id = self.ensure_message()
            if not part.id:
                part.id = _new_part_id()
            self._parts.append(part)
            effective_source = str(part.metadata.get("stream_source") or stream_source)
            self._publisher.publish(
                "message.part.added",
                {
                    "turn_id": self.turn_id,
                    "message_id": msg_id,
                    "stream_source": effective_source,
                    "part": part.to_wire(),
                },
            )
            return part

    def append_part_once(self, key: str, part: Part, **kw: Any) -> Optional[Part]:
        """:meth:`append_part` gated on a turn-scoped idempotency key.

        Replaces ``app.state.live_assistant_part_keys`` (used for
        ``route:{agent}`` banners and handoff parts). Returns ``None`` without
        touching the ledger when ``key`` was already appended this turn.
        """

        with self._lock:
            if self._frozen:
                self._audit_late_op("append_part_once", key=key, part_type=part.type)
                return None
            if key in self._once_keys:
                return None
            self._once_keys.add(key)
            return self.append_part(part, **kw)

    def has_part_key(self, key: str) -> bool:
        """True when :meth:`append_part_once` already consumed ``key`` this turn."""

        with self._lock:
            return key in self._once_keys

    def append_text_delta(self, agent_id: str, field: str, chunk: str) -> None:
        """Append a streamed text/thinking delta.

        Opens a new part (type ``thinking`` for ``provider_thinking:*`` fields,
        else ``text``) when ``(agent_id, field)`` changes — closing the prior
        part — publishes ``message.part.added`` on open and
        ``message.part.delta`` per chunk, plus the normalized twin
        (``turn.trace.delta`` for provider thinking, ``turn.text.delta``
        otherwise) from the same transition (design §4 rows 2-3).
        """

        if not chunk:
            return
        agent_id = str(agent_id or "")
        field = str(field or "answer")
        with self._lock:
            if self._frozen:
                self._audit_late_op("append_text_delta", agent_id=agent_id, field=field)
                return
            is_thinking = field.startswith(_PROVIDER_THINKING_PREFIX)
            self.ensure_message()
            part = self._open_part
            if part is None or agent_id != self._open_agent or field != self._open_field:
                self._close_open_text_locked()
                part = Part(
                    id=_new_part_id(),
                    type="thinking" if is_thinking else "text",
                    agent_id=agent_id,
                    text="",
                    metadata={
                        "stream_source": "live",
                        "signature_field_name": field,
                        **(
                            {
                                "thinking_source": "provider",
                                "provider_source": field.split(":", 1)[1],
                                "default_collapsed": True,
                            }
                            if is_thinking
                            else {}
                        ),
                    },
                )
                self._parts.append(part)
                self._buffers[part.id] = []
                self._open_part = part
                self._open_agent = agent_id
                self._open_field = field
                self._current_stream_part_id = part.id
                self._publisher.publish(
                    "message.part.added",
                    {
                        "turn_id": self.turn_id,
                        "message_id": self.message_id,
                        "stream_source": "live",
                        "part": part.to_wire(),
                    },
                )
            self._buffers[part.id].append(chunk)
            self._streamed.setdefault((agent_id, field), []).append(chunk)
            self._raw_stream.append(chunk)
            self._publisher.publish(
                "message.part.delta",
                {
                    "turn_id": self.turn_id,
                    "message_id": self.message_id,
                    "part_id": part.id,
                    "stream_source": "live",
                    "signature_field_name": field,
                    "delta": {"text_append": chunk},
                },
            )
            if is_thinking:
                provider_source = field.split(":", 1)[1] if ":" in field else "provider"
                self._publisher.publish(
                    "turn.trace.delta",
                    {
                        "turn_id": self.turn_id,
                        "trace_id": f"{self.turn_id}:{provider_source}",
                        "trace_kind": "model_aux",
                        "agent_id": agent_id,
                        "part_id": part.id,
                        "text_append": chunk,
                    },
                )
            else:
                self._publisher.publish(
                    "turn.text.delta",
                    {
                        "turn_id": self.turn_id,
                        "agent_id": agent_id,
                        "part_id": part.id,
                        "field": _transcript_text_field(field),
                        "text_append": chunk,
                    },
                )

    def close_open_text(self) -> None:
        """Close the open streamed part.

        Cleans the full buffer exactly once (text parts only; provider
        thinking is verbatim), DROPS the part when empty after clean (emits
        nothing — and reload cannot disagree, because the persisted list is
        this same ledger), else mutates ``part.text`` in place and publishes
        ``message.part.completed`` with the cleaned ``final_text``. Idempotent.
        """

        with self._lock:
            self._close_open_text_locked()

    def annotate(self, part_id: str, **metadata: Any) -> None:
        """Merge post-hoc facts into a part's metadata — never its text.

        For tool_result final previews, ``stream_fallback`` payloads,
        ``restates_part_id`` tags. Persisted with the part and republished as
        a metadata patch so live and reload agree.
        """

        with self._lock:
            if self._frozen:
                self._audit_late_op("annotate", part_id=part_id)
                return
            part = next((p for p in self._parts if p.id == part_id), None)
            if part is None:
                logger.warning(
                    "turn_transcript annotate rejected reason=unknown_part "
                    "session=%s turn=%s part=%s",
                    self.session_id,
                    self.turn_id,
                    part_id,
                )
                stream_audit(
                    "transcript.annotate_unknown_part",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    part_id=part_id,
                )
                return
            part.metadata.update(metadata)
            self._publisher.publish(
                "message.part.updated",
                {
                    "turn_id": self.turn_id,
                    "message_id": self.message_id,
                    "part_id": part_id,
                    "metadata_patch": dict(metadata),
                },
            )

    # -- state queries (identity checks replace content comparison) -----------

    def has_closed_text(self, agent_id: str, field: str = "answer") -> bool:
        """True when ``(agent_id, field)`` produced a non-empty closed part this turn."""

        with self._lock:
            return bool(self._closed_text.get((agent_id, field)))

    def streamed_text(self, agent_id: str, field: str) -> str:
        """Concatenated raw deltas for ``(agent_id, field)`` THIS turn."""

        with self._lock:
            return "".join(self._streamed.get((agent_id, field), []))

    def raw_streamed_text(self) -> str:
        """Every accepted streamed chunk THIS turn, concatenated in arrival order.

        Whole-turn, across agents and fields, provider thinking included —
        byte-identical to the legacy ``streamed_assistant_buffer`` join that
        the timeout / streaming-failure partial-answer paths report. (The
        per-field :meth:`streamed_text` cannot reproduce this concat, so PR2
        exposes the aggregate explicitly; disclosed against design §6 PR2.)
        """

        with self._lock:
            return "".join(self._raw_stream)

    @property
    def current_stream_part_id(self) -> Optional[str]:
        """Id of the last text part opened since the last runtime boundary.

        Legacy-equivalent of the ``streamed_assistant_part_id`` closure var:
        set when a streamed text/thinking part opens, cleared by an atomic
        :meth:`append_part` (the runtime boundary), and deliberately NOT
        cleared by :meth:`close_open_text` — finalize's ``stream_source``
        live-vs-batch provenance reads exactly that legacy semantic.
        """

        with self._lock:
            return self._current_stream_part_id

    def was_closed_live(self, part_id: str) -> bool:
        """True when ``part_id`` is a streamed part already closed live THIS turn.

        Includes empty-after-clean drops. The finalize publish loop uses this
        instead of the legacy ``closed_streamed_part_ids`` closure set so live
        parts are neither re-added nor re-completed.
        """

        with self._lock:
            return part_id in self._closed_live_part_ids

    def open_text_part(self) -> Optional[Part]:
        """The currently open streamed part, or ``None``."""

        with self._lock:
            return self._open_part

    def snapshot(self) -> list[Part]:
        """Read-only arrival-order copy of the ledger."""

        with self._lock:
            return list(self._parts)

    def live_parts_alias(self) -> list[Part]:
        """The INTERNAL ledger list, for the PR1/PR2 migration alias.

        Exposed as ``app.state.live_assistant_parts[sid]`` so untouched
        ``turn.py`` finalize reads keep working. Callers must treat it as
        read-only; the transcript mutates it in place.
        """

        return self._parts

    @property
    def frozen(self) -> bool:
        """True once :meth:`finalize` ran; late appends are rejected + audited."""

        with self._lock:
            return self._frozen

    # -- the exactly-once text channel (design §3.2) ---------------------------

    def field_stream(self, agent_id: str, field: str = "answer") -> "FieldStream":
        """Take the exactly-once text handle for ``(agent_id, field)``.

        LM-call sites take this BEFORE the call and :meth:`FieldStream.finish`
        it after; whether the batch fallback text lands is decided by op
        identity (did this handle stream?), never by comparing strings.
        """

        return FieldStream(self, agent_id, field)

    # -- the reader (finalize; loop-only) --------------------------------------

    def finalize(self) -> list[Part]:
        """Close open text, stamp 1-based sequence, freeze, return parts VERBATIM.

        No text rewriting. No dedup. No re-publish. Idempotent: a second call
        returns the same frozen ledger.
        """

        with self._lock:
            if not self._frozen:
                self._close_open_text_locked()
                for index, part in enumerate(self._parts, start=1):
                    part.sequence = index
                self._frozen = True
            return list(self._parts)

    def abandon(self) -> None:
        """Freeze the ledger WITHOUT closing open text or publishing anything.

        PR2-window settle: the legacy finalize region still owns the terminal
        wire events (it completes/republishes the open answer part itself), so
        turn end must retire the ledger without emitting — a transcript-side
        close here would double-publish ``message.part.completed``. Every turn
        exit path (success, the #756 finalize error envelope, the ask_user
        early return) calls this before ``registry.close(sid)`` so late
        producer ops are rejected + audited instead of silently absorbed.
        Idempotent. PR3 replaces this with :meth:`finalize` once the loop
        persists the ledger verbatim.
        """

        with self._lock:
            self._frozen = True

    # -- internals -------------------------------------------------------------

    def _close_open_text_locked(self) -> None:
        part = self._open_part
        if part is None:
            return
        self._open_part = None
        self._open_agent = ""
        open_field = self._open_field
        self._open_field = ""
        # Closed live (whether kept or dropped-empty below) — the finalize
        # publish loop consults this so live parts are never re-published.
        self._closed_live_part_ids.add(part.id)
        buffered = "".join(self._buffers.pop(part.id, []))
        # Clean the COMPLETE buffer exactly once, at close — never per chunk,
        # never again at finalize (the b1b25d2 invariant, now structural).
        if part.type == "text" and buffered.strip():
            buffered = self._clean_text(buffered)
        if not buffered.strip():
            # Empty after clean: remove from the ledger and emit nothing.
            # Identity-based removal — Part equality is by value and live
            # ledgers can hold equal-valued parts.
            for index, candidate in enumerate(self._parts):
                if candidate is part:
                    del self._parts[index]
                    break
            logger.info(
                "turn_transcript dropped_empty_part session=%s turn=%s part=%s type=%s",
                self.session_id,
                self.turn_id,
                part.id,
                part.type,
            )
            stream_audit(
                "transcript.dropped_empty_part",
                session_id=self.session_id,
                turn_id=self.turn_id,
                part_id=part.id,
                part_type=part.type,
            )
            return
        # Mutate in place: external alias views (app.state.live_assistant_parts)
        # must observe the close (design §9 alias-view aliasing).
        part.text = buffered
        self._closed_text.setdefault((part.agent_id, open_field), []).append(buffered)
        self._publisher.publish(
            "message.part.completed",
            {
                "turn_id": self.turn_id,
                "message_id": self.message_id,
                "part_id": part.id,
                "stream_source": "live",
                "final_text": buffered,
            },
        )

    def _append_batch_text_locked(self, agent_id: str, field: str, cleaned: str) -> Part:
        """One added+completed burst for a batch (non-streamed) text field.

        The wire shape matches today's finalize loop for batch parts:
        ``message.part.added`` then ``message.part.completed`` with
        ``stream_source: "batch"`` and NO delta events (design §4 row 7).
        """

        part = Part(
            id=_new_part_id(),
            type="text",
            agent_id=agent_id,
            text=cleaned,
            metadata={
                "stream_source": "batch",
                "signature_field_name": field,
            },
        )
        appended = self.append_part(part, stream_source="batch")
        assert appended is not None  # caller holds the lock and checked frozen
        self._closed_text.setdefault((agent_id, field), []).append(cleaned)
        self._publisher.publish(
            "message.part.completed",
            {
                "turn_id": self.turn_id,
                "message_id": self.message_id,
                "part_id": part.id,
                "stream_source": "batch",
                "final_text": cleaned,
            },
        )
        return part

    def _audit_late_op(self, op: str, **fields: Any) -> None:
        """A producer touched the ledger after finalize: reject loudly."""

        logger.warning(
            "turn_transcript late_op rejected op=%s session=%s turn=%s fields=%s",
            op,
            self.session_id,
            self.turn_id,
            fields,
        )
        stream_audit(
            "transcript.late_op",
            session_id=self.session_id,
            turn_id=self.turn_id,
            op=op,
            **fields,
        )


class FieldStream:
    """Exactly-once text channel for one ``(agent_id, field)`` within one LM-call scope.

    Take the handle BEFORE the LM call; the stream tap calls :meth:`append`;
    :meth:`finish` after the call decides — by op identity, never by string
    comparison — whether the batch ``fallback_text`` lands:

    - deltas streamed          -> close the part (clean once); fallback audited + ignored
    - no deltas + fallback     -> ONE added+completed batch burst authored to (agent, field)
    - neither                  -> ``None``
    """

    def __init__(self, transcript: TurnTranscript, agent_id: str, field: str) -> None:
        self._transcript = transcript
        self._agent_id = str(agent_id or "")
        self._field = str(field or "answer")
        self._streamed = False
        self._finished = False
        #: The part id this handle's text landed in; ``None`` until the first
        #: delta opens a part (or the batch burst lands one at finish).
        self.part_id: Optional[str] = None

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
                self._streamed = True
                self.part_id = open_part.id

    def finish(self, *, fallback_text: str = "") -> Optional[str]:
        """Settle the channel; returns the final text that landed, if any."""

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
            if self._streamed:
                if fallback_text.strip():
                    # The batch copy of an already-streamed field is dropped by
                    # IDENTITY (this handle streamed), never by text comparison;
                    # audited so parity data exists (#733's replacement).
                    stream_audit(
                        "transcript.fieldstream.fallback_ignored",
                        session_id=transcript.session_id,
                        turn_id=transcript.turn_id,
                        agent_id=self._agent_id,
                        field=self._field,
                        reason="already_streamed",
                        fallback_len=len(fallback_text),
                    )
                open_part = transcript._open_part
                if (
                    open_part is not None
                    and self.part_id is not None
                    and open_part.id == self.part_id
                ):
                    transcript._close_open_text_locked()
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
            cleaned = transcript._clean_text(fallback_text)
            if not cleaned.strip():
                stream_audit(
                    "transcript.dropped_empty_part",
                    session_id=transcript.session_id,
                    turn_id=transcript.turn_id,
                    part_id="",
                    part_type="text",
                )
                return None
            part = transcript._append_batch_text_locked(self._agent_id, self._field, cleaned)
            self.part_id = part.id
            return cleaned


class TurnTranscriptRegistry:
    """``app.state.turn_transcripts`` — one open :class:`TurnTranscript` per session.

    Lifecycle is owned by the turn loop: :meth:`open_turn` at turn start,
    :meth:`close` in the success path AND the finalize error envelope AND the
    ask_user early return — a leaked ledger must never poison the next turn
    (the failure class #757 fixed for the field-text buffer).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, TurnTranscript] = {}

    def open_turn(
        self,
        sid: str,
        turn_id: str,
        publisher: TranscriptPublisher,
        clean_text: Callable[[str], str],
    ) -> TurnTranscript:
        """Open the ledger for ``sid``'s new turn, evicting any leaked one loudly."""

        with self._lock:
            stale = self._by_session.pop(sid, None)
            if stale is not None:
                logger.warning(
                    "turn_transcript leaked ledger evicted reason=unclosed_prior_turn "
                    "session=%s stale_turn=%s new_turn=%s",
                    sid,
                    stale.turn_id,
                    turn_id,
                )
                stream_audit(
                    "transcript.leaked_ledger_evicted",
                    session_id=sid,
                    stale_turn_id=stale.turn_id,
                    turn_id=turn_id,
                )
            transcript = TurnTranscript(
                session_id=sid,
                turn_id=turn_id,
                publisher=publisher,
                clean_text=clean_text,
            )
            self._by_session[sid] = transcript
            return transcript

    def get(self, sid: str) -> Optional[TurnTranscript]:
        """The open transcript for ``sid``, or ``None`` (producers resolve here)."""

        with self._lock:
            return self._by_session.get(sid)

    def close(self, sid: str) -> None:
        """Retire ``sid``'s transcript at turn end (success, error, or early return)."""

        with self._lock:
            self._by_session.pop(sid, None)
