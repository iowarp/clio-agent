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
error envelope, and the ask_user early return).

PR3 scope (design §6): finalize is a pure READER. The turn loop appends its
finalize-time parts (routing banner, wrap-up thinking, the canonical answer
via :meth:`turn_answer_stream` + :meth:`FieldStream.finish`, file diffs)
through the same producer API and persists :meth:`finalize` VERBATIM — no
rewriting, no dedup, no re-publish loop. :class:`FieldStream` handles seed
their exactly-once identity from the turn's ledger state, so "did this
channel already produce the text?" is an op-identity check, never a string
comparison (replaces ``answer_already_present`` / ``answered_agents`` /
``expert_terminal_answers`` / the ``reuse_streamed_part_id`` text swap).

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

import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping
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


def _canonical_tool_args(args: Mapping[str, Any] | None) -> str:
    """Canonical JSON identity of a tool call's args, or ``""`` when unencodable.

    Sorted keys make two calls with the same argument dict compare equal
    regardless of insertion order; an unencodable dict yields the empty
    sentinel, which every caller treats as "no identity — never collapse".

    Generic helper — kept for any future caller that wants whole-args
    identity. The collector re-poll collapse does NOT use this (see
    :func:`_canonical_collector_key`): a collector's full args dict includes
    ``timeout_s``, which the model legitimately varies per re-poll.
    """

    try:
        return json.dumps(dict(args or {}), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


# Sentinel task-set identity for a collector call that polls EVERYTHING
# (``check_agent_tasks`` with ``task_ids`` omitted/``None``) — distinct from
# any real task id so it can never collide with a literal one-task poll.
_COLLECTOR_ALL_TASKS_KEY = "__all__"


def _canonical_collector_key(tool_name: str, args: Mapping[str, Any] | None) -> str:
    """Semantic re-poll identity for a collector call: tool name + task set.

    ``wait_agent_tasks`` / ``check_agent_tasks`` re-polls are the SAME
    logical activity — waiting on the same tasks — even when the per-poll
    ``timeout_s`` budget differs; a real turn re-polls the same task set with
    a DIFFERENT budget each time (round-6 live evidence: 60s then 90s), so
    canonicalizing the FULL args dict (as :func:`_canonical_tool_args` does)
    never collapses the exact case this feature exists for. The identity is
    therefore just the tool name plus the sorted ``task_ids`` (order-
    insensitive — a re-poll may list the remaining tasks in a different
    order); a missing/``None`` ``task_ids`` (``check_agent_tasks``'s "poll
    everything" call) canonicalizes to :data:`_COLLECTOR_ALL_TASKS_KEY`
    consistently, never to the empty-list identity of "polling zero tasks".
    """

    task_ids = (args or {}).get("task_ids")
    ids_key: Any = (
        _COLLECTOR_ALL_TASKS_KEY if task_ids is None else sorted(str(tid) for tid in task_ids)
    )
    try:
        return json.dumps([str(tool_name or ""), ids_key], sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def _collector_timeout_budget(args: Mapping[str, Any] | None) -> Optional[float]:
    """The requested ``timeout_s`` budget on a collector call's args, if any.

    ``check_agent_tasks`` carries no budget (it never blocks); ``None`` means
    "nothing to record", not "budget zero" — callers must not coerce it.
    """

    value = (args or {}).get("timeout_s")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _union_waited_tasks(prior: Any, new: Any) -> list[dict[str, Any]]:
    """Union two ``waited_tasks`` display-row lists by ``task_id`` (P5 wire
    semantics). A collapsed collector re-poll's canonical identity requires
    the SAME sorted ``task_ids`` on both attempts (:func:`_canonical_collector_key`),
    so in practice the two lists already describe the same task set — this
    guards the merge defensively (never a narrower result than either side)
    and lets the NEWEST attempt's row win per id when the two disagree, the
    same "newest attempt owns the facts" rule the rest of the collapse
    follows. Non-mapping / non-list inputs are treated as empty, never raise.
    """

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in (*(prior or []), *(new or [])):
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "")
        if task_id not in by_id:
            order.append(task_id)
        by_id[task_id] = dict(row)  # last write (from ``new``) wins per id
    return [by_id[task_id] for task_id in order]


class TranscriptFrozenError(RuntimeError):
    """A producer asked a FROZEN, never-minted ledger to mint the message id.

    Minting after :meth:`TurnTranscript.finalize` / :meth:`TurnTranscript.abandon`
    would attach a fresh assistant identity to a settled turn; returning an
    empty-string id instead would be a silent fallback. The caller gets a
    structured error and the op is audited (``transcript.late_op``).
    """


class TranscriptPublisher(Protocol):
    """Injected event sink for ledger transitions.

    The default implementation wraps the app :class:`EventBus` and emits the
    ``message.part.*`` transcript vocabulary from each ledger transition. The
    normalized ``turn.text.delta`` / ``turn.trace.delta`` twins this sink used
    to mirror were retired in #767 PR5 (they had zero consumers); ``message.part.*``
    (plus ``message.created`` / ``message.completed``) is now the sole transcript
    wire vocabulary.
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
    - Text is stored VERBATIM (#881): a streamed part carries the model's text
      byte-for-byte to the close event AND the part; the server binds no
      visible-text prose cleaner. The only close-time transform is dropping a
      part that is whitespace-only after buffering (it carries no content).
      ``finalize()`` never rewrites text.
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
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.message_id: str = ""
        self._publisher = publisher
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
        # (agent_id, field) -> chunks recorded SYNCHRONOUSLY by the LM stream tap in
        # the executor thread, BEFORE the cross-thread ``append_text_delta`` is
        # scheduled (#732) — the SAME thread the thought-dedup gate reads on.
        self._tap_streamed: dict[tuple[str, str], list[str]] = {}
        # Per-(agent,field) high-water cursor INTO _tap_streamed, advanced once per
        # tool-fire by the observer gate — carves the append-only tap bucket into
        # per-ReAct-step slices so step N is not latched by step N-1's chunks (#883).
        self._tap_gate_cursor: dict[tuple[str, str], int] = {}
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

    def upsert_delegation_part(self, part: Part, *, stream_source: str = "live") -> Optional[Part]:
        """Upsert one lifecycle phase without erasing another phase.

        Start and return are distinct chronological events. Observations of
        the same phase update in place so retries remain idempotent.
        """

        handle_id = str(part.handle_id or "")
        if not handle_id:
            return self.append_part(part, stream_source=stream_source)
        with self._lock:
            if self._frozen:
                self._audit_late_op("upsert_delegation_part", part_id=part.id, part_type=part.type)
                return None
            existing = next(
                (
                    row
                    for row in self._parts
                    if row.type == "expert_handoff"
                    and str(row.handle_id or "") == handle_id
                    and str(row.stage or "") == str(part.stage or "")
                ),
                None,
            )
            if existing is None:
                pass  # fall through to a plain append below, outside the lock
            else:
                merged_metadata = {**existing.metadata, **part.metadata}
                part.id = existing.id
                part.sequence = existing.sequence
                part.metadata = merged_metadata
                index = self._parts.index(existing)
                self._parts[index] = part
                msg_id = self.ensure_message()
                self._publisher.publish(
                    "message.part.updated",
                    {
                        "turn_id": self.turn_id,
                        "message_id": msg_id,
                        "stream_source": str(part.metadata.get("stream_source") or stream_source),
                        "part": part.to_wire(),
                    },
                )
                return part
        return self.append_part(part, stream_source=stream_source)

    def upsert_repeated_collector_call(
        self, part: Part, *, stream_source: str = "live"
    ) -> Optional[Part]:
        """A re-polled collector = ONE tool_call+tool_result pair (clean-wire rule).

        ``wait_agent_tasks`` / ``check_agent_tasks`` re-polls are one logical
        activity (waiting on the same tasks), not N transcript rows. A NEW
        ``tool_call`` whose SEMANTIC identity — tool name + sorted
        ``task_ids`` (:func:`_canonical_collector_key`), NOT the full args
        dict — equals the LAST ``tool_call`` part's, with nothing between
        them but that call's own ``tool_result``, REPLACES the prior call in
        place: same part id/sequence, ``metadata.attempts`` incremented,
        ``metadata.budgets`` appended with the re-poll's ``timeout_s`` (round-
        6 evidence: the model legitimately varies the wait budget per re-poll
        — 60s then 90s on the SAME task set — so the identity must ignore it
        or the collapse never fires on real turns), ``message.part.updated``
        published (:meth:`upsert_delegation_part` is the precedent). Its
        ``tool_result`` then replaces the prior result the same way, carrying
        cumulative ``metadata.attempts`` + ``metadata.total_wait_ms`` (the
        attempts' summed durations) while the VISIBLE content is the newest
        result VERBATIM — never a synthesized merge.

        Narration parts (``text`` AND ``thinking`` — ``_STREAMED_TEXT_TYPES``,
        the same narration lane) between re-polls never break the chain (owner
        amendment, round-4 live evidence: real turns interleave narration
        between EVERY re-poll, so strict adjacency made the collapse inert;
        round-5 live evidence — rerun sess_c6241fc8906f, msg_asst_8894cb745b15
        — showed the provider-thinking lane interleaved too, so a ``thinking``
        part between re-polls must be skipped exactly like ``text``). The
        narration parts stay exactly where they are — order preserved, never
        absorbed — while the duplicate pair is replaced in place at its
        ORIGINAL position. The chain DOES break on different args, any OTHER
        tool's call/result pair, or an ``expert_handoff`` between them:
        collapsing across those would reorder reality.

        The caller (the tool observer) scopes this to the two collector tools
        BY NAME; this method applies only the structural rule above — no
        prose inspection, no generic tool collapsing.
        """

        with self._lock:
            if self._frozen:
                self._audit_late_op(
                    "upsert_repeated_collector_call", part_id=part.id, part_type=part.type
                )
                return None
            # The collapse is an atomic-part runtime boundary exactly like
            # append_part: narration streamed since the last boundary closes
            # here (and stays where it is) — the re-poll never absorbs it and
            # later deltas never continue the pre-poll narration part.
            self._close_open_text_locked()
            self._current_stream_part_id = None
            if part.type == "tool_call":
                index = self._repeated_collector_call_index_locked(part)
            elif part.type == "tool_result":
                index = self._repeated_collector_result_index_locked(part)
            else:
                index = None
            if index is not None:
                existing = self._parts[index]
                merged_metadata = {**existing.metadata, **part.metadata}
                merged_metadata["attempts"] = int(existing.metadata.get("attempts") or 1) + 1
                if part.type == "tool_call":
                    if not part.thought:
                        # Keep the started reasoning under the re-poll (the same
                        # way the delegation upsert keeps the brief) when the
                        # new attempt carries none of its own.
                        part.thought = existing.thought
                    # Honest per-attempt detail (owner amendment, round-6): the
                    # collapse identity now ignores timeout_s, so record every
                    # attempt's requested budget explicitly rather than losing
                    # it silently. Absent when neither attempt carried one
                    # (e.g. check_agent_tasks, which never blocks).
                    prior_budgets = list(existing.metadata.get("budgets") or [])
                    if not prior_budgets:
                        first_budget = _collector_timeout_budget(existing.input)
                        if first_budget is not None:
                            prior_budgets = [first_budget]
                    new_budget = _collector_timeout_budget(part.input)
                    if new_budget is not None:
                        prior_budgets.append(new_budget)
                    if prior_budgets:
                        merged_metadata["budgets"] = prior_budgets
                    # A collapsed wait covering two attempts on the SAME task set
                    # (P5 wire semantics) must never present FEWER resolved
                    # ``waited_tasks`` rows than either attempt saw — union by
                    # task_id rather than the generic ``{**existing, **new}``
                    # merge's plain overwrite.
                    prior_waited = existing.metadata.get("waited_tasks")
                    new_waited = part.metadata.get("waited_tasks")
                    if prior_waited is not None or new_waited is not None:
                        merged_metadata["waited_tasks"] = _union_waited_tasks(
                            prior_waited, new_waited
                        )
                if part.type == "tool_result":
                    prior_total = existing.metadata.get("total_wait_ms")
                    if prior_total is None:
                        prior_total = existing.duration_ms
                    merged_metadata["total_wait_ms"] = float(prior_total or 0.0) + float(
                        part.duration_ms or 0.0
                    )
                    # The newest attempt owns the result facts: a prior
                    # attempt's evidence must not survive under a newer
                    # attempt that lacks it (e.g. a failed re-poll). The
                    # TOP-LEVEL ``structured_content`` field (#1190) needs no
                    # scrub here: the replacing part is stored wholesale below,
                    # so the newest attempt's value (or absence) already wins.
                    if "result" in merged_metadata and "result" not in part.metadata:
                        merged_metadata.pop("result")
                part.id = existing.id
                part.sequence = existing.sequence
                part.metadata = merged_metadata
                # In-place list mutation (never rebound) — alias views observe it.
                self._parts[index] = part
                msg_id = self.ensure_message()
                self._publisher.publish(
                    "message.part.updated",
                    {
                        "turn_id": self.turn_id,
                        "message_id": msg_id,
                        "stream_source": str(part.metadata.get("stream_source") or stream_source),
                        "part": part.to_wire(),
                    },
                )
                return part
        return self.append_part(part, stream_source=stream_source)

    def _repeated_collector_call_index_locked(self, part: Part) -> Optional[int]:
        """Ledger index of the prior same-args collector call ``part`` replaces.

        Walks back from the tail to the LAST ``tool_call``, skipping narration
        ``text`` AND ``thinking`` parts (``_STREAMED_TEXT_TYPES`` — both are
        the narration lane, same order-preservation rationale: they never
        break the chain because the pair collapses at its original position
        and the narration stays exactly where it streamed). The call must
        carry the same SEMANTIC identity — tool name + sorted ``task_ids``
        (:func:`_canonical_collector_key`; deliberately NOT the full args
        dict, so a re-poll with a different ``timeout_s`` still collapses),
        and every part after it must be that call's own ``tool_result`` or
        narration text/thinking. Anything else — a different task set,
        another tool's call/result, an ``expert_handoff``, any other part
        type — yields ``None`` and the caller appends normally.
        """

        new_key = _canonical_collector_key(part.tool_name, part.input)
        if not new_key:
            return None
        for index in range(len(self._parts) - 1, -1, -1):
            candidate = self._parts[index]
            if candidate.type == "tool_call":
                if candidate.tool_name != part.tool_name:
                    return None
                if _canonical_collector_key(candidate.tool_name, candidate.input) != new_key:
                    return None
                for trailing in self._parts[index + 1 :]:
                    if trailing.type in _STREAMED_TEXT_TYPES:
                        continue
                    if trailing.type == "tool_result" and trailing.call_id == candidate.call_id:
                        continue
                    return None
                return index
            if candidate.type not in ("tool_result", *_STREAMED_TEXT_TYPES):
                return None
        return None

    def _repeated_collector_result_index_locked(self, part: Part) -> Optional[int]:
        """Ledger index of the prior collector result ``part`` replaces.

        Matches the shape the sibling call-upsert leaves behind — the collapsed
        ``tool_call`` (carrying THIS result's call_id) immediately followed by
        the PRIOR attempt's ``tool_result`` — reached by walking back over any
        narration ``text``/``thinking`` that streamed after the pair
        (``_STREAMED_TEXT_TYPES`` — both lanes stay put, same as the sibling
        call-upsert). Anything else yields ``None`` (append normally: an
        uncollapsed call sits at the tail with nothing after it, so its
        result never matches here).
        """

        if not str(part.call_id or ""):
            return None
        for index in range(len(self._parts) - 1, -1, -1):
            candidate = self._parts[index]
            if candidate.type == "tool_call":
                if candidate.tool_name != part.tool_name:
                    return None
                if str(candidate.call_id or "") != str(part.call_id or ""):
                    return None
                if index + 1 >= len(self._parts):
                    return None
                prior = self._parts[index + 1]
                if prior.type != "tool_result" or prior.tool_name != part.tool_name:
                    return None
                if str(prior.call_id or "") == str(part.call_id or ""):
                    return None
                return index + 1
            if candidate.type not in ("tool_result", *_STREAMED_TEXT_TYPES):
                return None
        return None

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

    def mark_part_key(self, key: str) -> bool:
        """Consume a turn-scoped once-key WITHOUT appending a part.

        For emissions that moved off the transcript (routing decisions became
        semantic events — clean-wire rule) but keep the same once-per-turn
        identity the part had. Returns ``False`` when already consumed.
        """

        with self._lock:
            if key in self._once_keys:
                return False
            self._once_keys.add(key)
            return True

    def append_text_delta(self, agent_id: str, field: str, chunk: str) -> None:
        """Append a streamed text/thinking delta.

        Opens a new part (type ``thinking`` for ``provider_thinking:*`` fields,
        else ``text``) when ``(agent_id, field)`` changes — closing the prior
        part — publishes ``message.part.added`` on open and
        ``message.part.delta`` per chunk (design §4 rows 2-3).
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

    def promote_open_text_field(
        self,
        agent_ids: Iterable[str],
        *,
        source_field: str,
        target_field: str,
    ) -> bool:
        """Move the current streamed text part to another contract field.

        ReAct exposes its prose through ``next_thought`` before the provider's
        tool-call list is known. When that list is empty, the same model-owned
        part is the terminal answer. Promote that open part by producer identity
        so the ledger keeps one text part rather than appending a batch copy.

        No text is compared or rewritten. Only the currently open part can move,
        and the completed-part metadata patch keeps the live and reload views in
        agreement.
        """

        allowed_agents = {str(agent_id or "") for agent_id in agent_ids} - {""}
        source = str(source_field or "")
        target = str(target_field or "")
        if not allowed_agents or not source or not target or source == target:
            return False
        with self._lock:
            if self._frozen:
                self._audit_late_op(
                    "promote_open_text_field",
                    source_field=source,
                    target_field=target,
                )
                return False
            part = self._open_part
            if (
                part is None
                or part.type != "text"
                or self._open_field != source
                or self._open_agent not in allowed_agents
            ):
                return False
            agent_id = self._open_agent
            self._close_open_text_locked()
            source_key = (agent_id, source)
            closed = self._closed_text.get(source_key)
            if not closed or part not in self._parts:
                return False
            landed = closed.pop()
            if not closed:
                self._closed_text.pop(source_key, None)
            self._closed_text.setdefault((agent_id, target), []).append(landed)
            part.metadata["signature_field_name"] = target
            self._publisher.publish(
                "message.part.updated",
                {
                    "turn_id": self.turn_id,
                    "message_id": self.message_id,
                    "part_id": part.id,
                    "metadata_patch": {"signature_field_name": target},
                },
            )
            return True

    def discard_open_text(self) -> bool:
        """Abandon the open streamed part WITHOUT closing/publishing it (D15).

        Sibling of :meth:`close_open_text` for an attempt that never counted: the
        LM transient-retry boundary (``lm.io_logging``) re-issues a call through
        a fresh field extractor with no memory of a failed attempt, and
        ``append_text_delta`` keeps re-using the open ``(agent_id, field)`` part
        -- without this, the retry's text lands on top of the abandoned
        attempt's in the SAME part (the duplicated-paragraph defect observed
        live, ``sess_539d24da07bf`` ``part_2b645566433b``). Unconditionally
        removes the part from ``self._parts`` (never a ``message.part.completed``
        publish) -- append-only/no-rewrite still holds; it never counted. A
        no-op when nothing is open, or frozen (audited, never silently absorbed).
        """

        with self._lock:
            if self._frozen:
                self._audit_late_op("discard_open_text")
                return False
            part = self._open_part
            if part is None:
                return False
            self._open_part = None
            self._open_agent = ""
            self._open_field = ""
            self._current_stream_part_id = None
            buffered = "".join(self._buffers.pop(part.id, []))
            for index, candidate in enumerate(self._parts):
                if candidate is part:
                    del self._parts[index]
                    break
            logger.info("transcript discarded_retry_part part=%s chars=%d", part.id, len(buffered))
            stream_audit(
                "transcript.discarded_retry_part",
                session_id=self.session_id,
                turn_id=self.turn_id,
                part_id=part.id,
                part_type=part.type,
                chunk_len=len(buffered),
                head=buffered[:120],
            )
            return True

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

    def record_streamed_field_text(self, agent_id: str, field: str, chunk: str) -> None:
        """Record a streamed contract-field ``chunk`` SYNCHRONOUSLY from the LM tap.

        The LM stream tap runs in the expert's executor thread and schedules the
        visible delta onto the loop cross-thread. The thought-dedup gate runs in
        that SAME executor thread when a tool fires, so the tap records here first,
        in-thread, giving the gate a source with a real happens-before (#732).
        Turn-scoped by construction, so a prior turn never leaks in.
        """

        if not chunk:
            return
        key = (str(agent_id or ""), str(field or "answer"))
        with self._lock:
            self._tap_streamed.setdefault(key, []).append(chunk)

    def tap_step_survives_clean(self, agent_id: str, field: str) -> tuple[bool, bool]:
        """Per-step (consumed) tap classification for the #883 thought-dedup gate.

        Returns ``(had_stream, survives_clean)`` for the tap slice since the LAST
        call for ``(agent_id, field)`` — a per-key cursor carves the append-only
        ``_tap_streamed`` bucket into ReAct steps. Computed SYNCHRONOUSLY in the
        caller's thread with NO cross-thread-close dependency (why ``has_closed_text``
        is rejected: it reads False for a not-yet-closed non-empty row -> double
        render). Since #881 the transcript stores text VERBATIM, so "survives as a
        visible row" is exactly "has non-whitespace content" — the SAME whitespace-
        only close drop :meth:`_close_open_text_locked` applies (the DSPy contract
        markers that used to empty a slice are split off at the root, #877, and can
        no longer reach a field's streamed text). CONSUMING READ — called EXACTLY
        ONCE per tool-fire (the observer gate).
        """

        key = (agent_id, field)
        with self._lock:
            chunks = self._tap_streamed.get(key, [])
            start = self._tap_gate_cursor.get(key, 0)
            self._tap_gate_cursor[key] = len(chunks)
            tail = "".join(chunks[start:])
        survived = bool(tail.strip())
        return (survived, survived)

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

        Includes empty-after-clean drops. Since PR3 deleted the finalize
        re-publish loop this is a diagnostic query only (closing is a ledger
        state transition; finalize publishes nothing).
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

        LM-call sites take this around the call and :meth:`FieldStream.finish`
        it after; whether the batch fallback text lands is decided by op
        identity (did this channel already produce a part this turn?), never
        by comparing strings. The handle seeds that identity from the turn's
        ledger state, so a handle taken after the call (the stream tap feeds
        :meth:`append_text_delta` directly) still sees its channel's streamed
        part.
        """

        return FieldStream(self, agent_id, field)

    def turn_answer_stream(self, responder_agent_id: str, *also_covering: str) -> "FieldStream":
        """The finalize-scoped exactly-once handle for the turn's canonical answer.

        The channel covers the AGENT LABELS the turn's top-level answer can
        stream under (#767 PR3, mechanism 5's replacement): the routed
        responder plus the stream tap's attribution fallbacks — the chat path
        labels chunks with the active/session agent while
        ``pred.selected_expert`` names the responder, and both are the SAME
        LM call's answer field. When a part already landed on any covered
        label, :meth:`FieldStream.finish` closes/keeps that part and the batch
        fallback is audited + ignored by identity; when none landed, ONE batch
        burst authored to ``responder_agent_id`` lands. Never both, never a
        text swap. A delegated child's own answer channel is NOT covered — its
        deliverable settles at its LM-call site and must never suppress the
        responder's distinct final answer.
        """

        covers = frozenset({responder_agent_id, *also_covering} - {""}) or frozenset(
            {responder_agent_id}
        )
        return FieldStream(self, responder_agent_id, "answer", covers=covers)

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

        The non-emitting settle: the ask_user early return carries the
        in-flight assistant state across the question (nothing may publish or
        close), and the #756 finalize error envelope must not emit transcript
        events for a turn it is settling as failed. Every turn exit path calls
        this before ``registry.close(sid)`` so late producer ops are rejected
        + audited instead of silently absorbed; on the success path it is a
        no-op because :meth:`finalize` already froze the ledger. Idempotent.
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
        # #881: text is stored VERBATIM — no visible-text prose cleaner runs here.
        # The buffer is the model's text byte-for-byte; the only transform is the
        # whitespace-only drop below (a part with no content emits nothing).
        if not buffered.strip():
            # Whitespace-only: remove from the ledger, emit nothing. Identity-based
            # removal -- Part equality is by value and live ledgers can hold dupes.
            for index, candidate in enumerate(self._parts):
                if candidate is part:
                    del self._parts[index]
                    break
            (logger.warning if part.type == "thinking" else logger.info)(  # gact-tui#362
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
                chars=len(buffered),
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

    def _append_batch_text_locked(
        self,
        agent_id: str,
        field: str,
        cleaned: str,
        *,
        extra_metadata: Optional[Mapping[str, Any]] = None,
    ) -> Part:
        """One added+completed burst for a batch (non-streamed) text field.

        The wire shape matches the retired finalize loop's batch parts:
        ``message.part.added`` then ``message.part.completed`` with
        ``stream_source: "batch"`` and NO delta events (design §4 row 7).
        ``extra_metadata`` carries post-hoc facts that ride the burst (e.g.
        the turn's ``stream_fallback`` payload); a ``stream_fallback`` key is
        mirrored onto the completed event to keep the legacy completed shape.
        """

        part = Part(
            id=_new_part_id(),
            type="text",
            agent_id=agent_id,
            text=cleaned,
            metadata={
                "stream_source": "batch",
                "signature_field_name": field,
                **dict(extra_metadata or {}),
            },
        )
        appended = self.append_part(part, stream_source="batch")
        assert appended is not None  # caller holds the lock and checked frozen
        self._closed_text.setdefault((agent_id, field), []).append(cleaned)
        completed_payload: dict[str, Any] = {
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "part_id": part.id,
            "stream_source": "batch",
            "final_text": cleaned,
        }
        if part.metadata.get("stream_fallback"):
            completed_payload["stream_fallback"] = part.metadata["stream_fallback"]
        self._publisher.publish("message.part.completed", completed_payload)
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
        transcript: TurnTranscript,
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
            elif self._landed_locked():
                self.part_id = next(
                    (
                        part.id
                        for part in reversed(transcript._parts)
                        if part.type == "text"
                        and part.agent_id in self._covers
                        and part.metadata.get("signature_field_name") == self._field
                    ),
                    None,
                )

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
                    stream_audit(
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
