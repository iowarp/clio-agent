"""Live runtime context: project ARC's ONE semantic-event log into turn records.

The canonical semantic-event trace is the source of truth for "what happened"
in a turn. Historically ARC rebuilt Invocation/Conversation records *post hoc*
from dspy Predictions and run traces -- a second, divergent recorder. This
module projects the SAME events the durable trace captures -- as persisted by
ARC into its ONE ``_events`` log (``semantic_event`` segments) -- so ARC's
Invocation/Conversation become **projections** of that log instead of
independent builds.

Design (ARC unification — one log):
    * There is NO separate ``self._sessions`` dict and NO separate folded copy.
      The observer is a pure READER over the SINGLE persisted semantic-event log:
      the ``_events`` scope of the ONE :class:`~clio_agent.arc.segments.SegmentStore`,
      where :meth:`ARCMemory._record_event_segment` appends one lean
      ``semantic_event`` segment per recorded event.
    * The reads (``view`` / ``project_conversation`` / ``project_invocations``)
      are QUERIES over that log: ``render`` the ``_events`` scope, keep the
      ``semantic_event`` segments, GROUP by ``turn_id`` (first-seen order),
      REPLAY each turn's events in render = (order, logical_time) order through
      the SAME per-turn reducer (:func:`_apply`), which reads the event's content
      fields (``event_type`` / ``payload`` / ``actor`` / ``provider`` / ``status``
      / ``occurred_at``) -- exactly the extraction the old fold did from the raw
      event -- and rebuilds the lean per-turn aggregate. The projection
      construction that aggregate feeds is unchanged.
    * The ``_events`` scope is INVISIBLE to the expert-prompt render: it is its own
      scope, so a working-set render of an expert scope never sees it, and
      ``semantic_event`` is not a working-set kind nor part of the dspy trajectory
      projection.
    * Released per session (``release``) and wholesale (``clear``) -- the
      ``_events`` scope's segments are ERASED (``drop_scope``) so an idle server
      returns to baseline. Erasing is only safe when the durable trace keeps the
      full history, so ``ARCMemory`` calls these ONLY when the durable trace
      backend is enabled; under the default ``none`` backend the log is the only
      copy and is retained instead (#762).

``project_conversation`` / ``project_invocations`` produce valid
``clio_agent.arc.schema`` objects from the log; ``view`` is the compact summary
``context_compiler`` reads for the open session.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

from clio_agent.arc.schema import Conversation, Invocation, Message

# ``_encode_safe`` lives in ``segments.py`` (the lowest write chokepoint, so the generic
# segment write path can coerce content without a circular import) and is re-exported
# here so the event path (:func:`build_event_content`) and existing importers of
# ``clio_agent.arc.live._encode_safe`` keep their surface unchanged.
from clio_agent.arc.segments import SegmentStore, _encode_safe

# Reserved scope that holds ARC's ONE persisted semantic-event log. It is its OWN
# scope, so an expert/working-set render (which renders a specific expert scope)
# never sees it; combined with ``semantic_event`` not being a working-set kind, the
# log can never leak into a model prompt. Defined here (the observer's substrate) and
# re-exported by ``arc.memory`` (the writer) so both share one constant.
#
# The log is a CHUNK FAMILY, not one ever-growing scope: chunk 1 is the legacy bare
# ``_events`` and chunk ``N>=2`` is ``_events/N``. The writer rolls to the next chunk
# once the active one reaches ``arc.events_chunk_segments`` segments, so a single
# append re-encodes only the active chunk (O(chunk)) instead of the whole log (O(N)),
# killing the per-event O(N²) rewrite. The reads concatenate ``render`` across the
# family in chunk order; the store-wide monotonic ``logical_time`` guarantees that
# concatenation equals the old single-scope render (each chunk is filled to capacity
# before the next opens, so every chunk-1 event precedes every chunk-2 event).
EVENTS_SCOPE = "_events"


def events_chunk_scope(index: int) -> str:
    """Scope name for the ``index``-th chunk of the reserved ``_events`` log.

    Chunks are 1-indexed. Chunk 1 is the legacy bare ``_events`` (so existing single-
    scope logs keep working unchanged); chunk ``N>=2`` is ``_events/N``. THE single
    naming rule, shared by the writer (:meth:`ARCMemory._append_event_segment`) and the
    reader (:func:`events_chunk_index` / :meth:`LiveRuntimeContext.events_scopes`).
    """
    return EVENTS_SCOPE if index <= 1 else f"{EVENTS_SCOPE}/{index}"


def events_chunk_index(scope: str) -> int:
    """Chunk index of an ``_events`` family scope (inverse of :func:`events_chunk_scope`).

    ``_events`` -> 1; ``_events/N`` -> N. A non-family scope (or an unparsable tail)
    maps to 1 so ordering is total and never raises.
    """
    if scope == EVENTS_SCOPE:
        return 1
    prefix = f"{EVENTS_SCOPE}/"
    if scope.startswith(prefix):
        try:
            return int(scope[len(prefix) :])
        except ValueError:
            return 1
    return 1


def is_events_scope(scope: str) -> bool:
    """Whether ``scope`` belongs to the reserved ``_events`` chunk family.

    True for the bare ``_events`` and every ``_events/N`` chunk. Used to keep the whole
    family out of the search-text companion (the log must never pollute scope search)
    and to enumerate the family for lifecycle erase / concatenated reads.
    """
    return scope == EVENTS_SCOPE or scope.startswith(f"{EVENTS_SCOPE}/")


def build_event_content(event: Any) -> Optional[dict[str, Any]]:
    """Canonical content dict for ONE ``semantic_event`` segment, or ``None`` for an
    untyped event. THE single builder shared by the production writer
    (``ARCMemory._append_event_segment``) and the standalone observer
    (``LiveRuntimeContext._record``), so the persisted ``_events`` log is identical
    regardless of path.

    Stores the event VERBATIM — NO truncation, NO caps. ARC is the source and holds
    everything (freeze-anytime); any bound is a downstream consumer's deliberate,
    configurable choice, never imposed here. The read-time reducer :func:`_apply`
    consumes ``event_type`` / ``payload`` / ``actor`` / ``provider`` / ``status`` /
    ``occurred_at`` / ``trace_id``; ``summary`` / ``subject`` are kept for completeness.

    The structured fields (``actor`` / ``subject`` / ``payload`` / ``provider``) are run
    through :func:`_encode_safe`, which recursively coerces any non-native value (litellm
    usage objects, pydantic models, dataclasses, sets/tuples, …) to a plain serializable
    form. This guarantees ARC's strict msgpack encode NEVER throws on an exotic payload
    from ANY emit site, so no semantic event is ever silently dropped from ARC.
    """
    etype = str(getattr(event, "event_type", "") or "")
    if not etype:
        return None
    return {
        "event_type": etype,
        "status": str(getattr(event, "status", "") or ""),
        "summary": str(getattr(event, "summary", "") or ""),
        "actor": _encode_safe(getattr(event, "actor", {}) or {}),
        "subject": _encode_safe(getattr(event, "subject", {}) or {}),
        "payload": _encode_safe(getattr(event, "payload", {}) or {}),
        "provider": _encode_safe(getattr(event, "provider", {}) or {}),
        "occurred_at": str(getattr(event, "occurred_at", "") or ""),
        "trace_id": str(getattr(event, "trace_id", "") or ""),
    }


_NO_TURN = "_no_turn"


def _epoch(iso: str) -> float:
    """Parse an ISO-8601 occurred_at to epoch seconds (0.0 on failure)."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


@dataclass
class _LiveExpert:
    """Lean per-expert sub-state within a turn."""

    agent_id: str
    answer: str = ""
    reasoning_len: int = 0
    trajectory_steps: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    provider: dict[str, Any] = field(default_factory=dict)


@dataclass
class _LiveTurn:
    """Lean aggregate of one turn's events (rebuilt at read-time by replaying the
    turn's ``semantic_event`` segments through :func:`_apply`)."""

    turn_id: str
    trace_id: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    status: str = "running"
    question: str = ""
    answer: str = ""
    selected_expert: str = ""
    route_reason: str = ""
    experts: "OrderedDict[str, _LiveExpert]" = field(default_factory=OrderedDict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def duration_ms(self) -> float:
        if self.started_at and self.completed_at and self.completed_at >= self.started_at:
            return (self.completed_at - self.started_at) * 1000.0
        return 0.0


class _MemoryStore:
    """Minimal in-memory :class:`~clio_agent.arc.storage.ARCStore` for a standalone
    :class:`LiveRuntimeContext` (the no-arg constructor used by unit tests / a
    memory-free observer). ARCMemory injects its own SegmentStore over the real
    persistence seam, so this is only the default backing when none is provided."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    def put(
        self, kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: Any = None
    ) -> None:
        self._data[(kind, name)] = data

    def get(self, kind: str, name: str) -> Optional[bytes]:
        return self._data.get((kind, name))

    def exists(self, kind: str, name: str) -> bool:
        return (kind, name) in self._data

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for (k, name), data in list(self._data.items()):
            if k == kind and name.startswith(prefix):
                yield name, data

    def delete(self, kind: str, name: str) -> None:
        self._data.pop((kind, name), None)

    def clear(self) -> None:
        self._data.clear()

    def supports_search(self) -> bool:
        return False

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        return []


class LiveRuntimeContext:
    """Live projection of the semantic-event log, backed by the ONE SegmentStore.

    The observer is a pure READER over the ``_events`` scope of the injected
    :class:`~clio_agent.arc.segments.SegmentStore` -- the single log into which
    :meth:`ARCMemory._record_event_segment` persists one ``semantic_event`` segment
    per recorded event. The reads query+group+replay that scope. Thread-safety is
    inherited from the SegmentStore's per-scope locking.
    """

    def __init__(self, store: SegmentStore | None = None) -> None:
        """Back the observer with a SegmentStore.

        Args:
            store: The SegmentStore the observer READS its ``_events`` scope from.
                ARCMemory passes ``self._segments`` so the observer projects over the
                same buffer (and highway op log) that the writer persists the log into.
                When ``None`` (standalone / unit tests), a private in-memory-backed
                SegmentStore is created so the observer is self-contained — and
                :meth:`fold` writes the log to it so the tests' projections work.
        """
        if store is None:
            # A standalone observer keeps the ``_events`` chunk family out of the
            # search-text companion too (parity with the production store ARCMemory
            # injects), so the log can never surface in scope search.
            store = SegmentStore(_MemoryStore(), search_indexed=lambda scope: not is_events_scope(scope))
        self._segments = store

    # ---- ingest (standalone / test convenience) ------------------------

    def fold(self, event: Any) -> None:
        """Persist one RAW SemanticEvent into the ``_events`` log (test convenience).

        In production ARCMemory is the writer (``_record_event_segment``); this method
        lets a STANDALONE observer (its own SegmentStore) be fed events directly so it
        has a log to project over. Append-only, correlated by ``turn_id``. Best-effort
        -- a live record must never break a turn.
        """
        try:
            self._record(event)
        except Exception:  # noqa: BLE001,S110 - a live record must never break a turn
            pass

    def _record(self, event: Any) -> None:
        sid = str(getattr(event, "session_id", "") or "")
        if not sid:
            return
        content = build_event_content(event)
        if content is None:  # an event with no event_type -> nothing to log
            return
        self._segments.append(
            sid,
            EVENTS_SCOPE,
            "semantic_event",
            content,
            turn_id=str(getattr(event, "turn_id", "") or ""),
            expert_span_id=str(getattr(event, "expert_span_id", "") or ""),
        )

    # ---- chunk family --------------------------------------------------

    def events_scopes(self, session_id: str) -> list[str]:
        """The session's ``_events`` chunk scopes, in chunk order (``[]`` if none).

        Discovers the persisted family via ``scan_scopes`` and orders it by chunk
        index, so the reads can concatenate ``render`` across it and the lifecycle can
        erase the whole family. Chunk order is also global event order (each chunk is
        filled before the next opens; ``logical_time`` is store-wide monotonic)."""
        scopes = [
            s for s in self._segments.scan_scopes(session_id, EVENTS_SCOPE) if is_events_scope(s)
        ]
        return sorted(scopes, key=events_chunk_index)

    # ---- lifecycle -----------------------------------------------------

    def release(self, session_id: str) -> int:
        """Drop a session's live turns (erase the whole ``_events`` chunk family).
        Returns the number of turns released (NOT segments), matching the historical
        contract."""
        turn_count = len(self._turns(session_id))
        for scope in self.events_scopes(session_id):
            self._segments.drop_scope(session_id, scope)
        return turn_count

    def clear(self) -> None:
        """Erase the ``_events`` log (every chunk of every session) — idle -> baseline."""
        for session_id in self._event_session_ids():
            for scope in self.events_scopes(session_id):
                self._segments.drop_scope(session_id, scope)

    def _event_session_ids(self) -> list[str]:
        """Every session that currently holds an ``_events`` record (so ``clear`` can
        erase them all). Chunk 1 (the bare ``_events``) is always present for any session
        with events — it is created first and only dropped with the whole family — so
        enumerating by it covers every session that owns any chunk."""
        return self._segments.sessions_with_scope(EVENTS_SCOPE)

    # ---- read / replay -------------------------------------------------

    def _turns(self, session_id: str) -> "OrderedDict[str, _LiveTurn]":
        """Rebuild ALL of a session's turns by querying the ``_events`` log, keeping the
        ``semantic_event`` segments, GROUPING them by ``turn_id`` (first-seen order), and
        REPLAYING each turn's events (in render = (order, logical_time) order, which is
        record order) through :func:`_apply`. NO turn cap — ARC holds every turn; a
        consumer that wants only a recent window asks for it explicitly (see
        :meth:`view`'s ``max_turns``).

        The log is a chunk family: CONCATENATE ``render`` across the chunks in chunk
        order. Each chunk is rendered internally by ``(order, logical_time)``; because a
        chunk is filled to capacity before the next opens, chunk order IS global event
        order, so the concatenation equals the old single-scope render byte-for-byte."""
        segments: list[Any] = []
        for scope in self.events_scopes(session_id):
            segments.extend(self._segments.render(session_id, scope))
        grouped: "OrderedDict[str, list[Any]]" = OrderedDict()
        for seg in segments:
            if seg.kind != "semantic_event":
                continue
            grouped.setdefault(seg.turn_id or _NO_TURN, []).append(seg)
        turns: "OrderedDict[str, _LiveTurn]" = OrderedDict()
        for turn_id, segs in grouped.items():
            turn = _LiveTurn(turn_id=turn_id)
            for seg in segs:
                _apply(turn, seg.content)
            turns[turn_id] = turn
        return turns

    def iter_event_contents(self) -> Iterator[dict[str, Any]]:
        """Yield every persisted ``_events`` segment content dict, in record order.

        A pure reader over the log for projections that fold ALL events at boot
        (e.g. the artifact registry rebuild, #966 S1): iterate each session's
        ``_events`` chunk family in chunk order and yield the ``semantic_event``
        content dicts (the :func:`build_event_content` form). Categorization the
        consumer needs (``event_type``/``payload``/``subject``) rides the content;
        session/turn envelope fields ride the segment, not the content (§2.3), so a
        consumer needing them reads per-session instead.
        """
        for session_id in self._event_session_ids():
            for scope in self.events_scopes(session_id):
                for seg in self._segments.render(session_id, scope):
                    if seg.kind != "semantic_event":
                        continue
                    if isinstance(seg.content, dict):
                        yield seg.content

    def view(self, session_id: str, *, max_turns: Optional[int] = None) -> dict[str, Any]:
        """Summary of a session's turns (for context_compiler). ``max_turns`` is an
        OPTIONAL recent-window the CALLER may pass (its own, configurable prompt budget);
        ``None`` (default) returns EVERY turn — there is no hardcoded cap here."""
        turns = self._turns(session_id)
        if not turns:
            return {}
        values = list(turns.values())
        recent = values[-max_turns:] if max_turns else values
        return {
            "session_id": session_id,
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "question": t.question,
                    "answer": t.answer,
                    "selected_expert": t.selected_expert,
                    "status": t.status,
                    "experts": list(t.experts.keys()),
                    "tools": [r.get("tool") for r in t.tools],
                }
                for t in recent
            ],
        }

    def project_conversation(self, session_id: str, *, user_id: str = "") -> Optional[Conversation]:
        """Project the session's turns into a Conversation (Q/A message pairs)."""
        turns = self._turns(session_id)
        if not turns:
            return None
        messages: list[Message] = []
        first_ts = 0.0
        last_ts = 0.0
        for t in turns.values():
            if t.question:
                messages.append(
                    Message(role="user", content=t.question, timestamp=t.started_at or 0.0)
                )
            if t.answer:
                messages.append(
                    Message(
                        role="assistant",
                        content=t.answer,
                        timestamp=t.completed_at or 0.0,
                        metadata={"selected_expert": t.selected_expert, "status": t.status},
                    )
                )
            first_ts = first_ts or t.started_at
            last_ts = t.completed_at or t.started_at or last_ts
        return Conversation(
            session_id=session_id,
            user_id=user_id,
            created_at=first_ts or 0.0,
            updated_at=last_ts or 0.0,
            last_accessed=last_ts or 0.0,
            status="active",
            messages=messages,
        )

    def project_invocations(self, session_id: str) -> list[Invocation]:
        """Project the session's turns into per-expert Invocation records."""
        turns = self._turns(session_id)
        if not turns:
            return []
        out: list[Invocation] = []
        for t in turns.values():
            # One tier-2 invocation per expert that produced a response.
            for exp in t.experts.values():
                out.append(
                    Invocation(
                        trace_id=f"{t.trace_id or t.turn_id}:{exp.agent_id}",
                        session_id=session_id,
                        parent_trace_id=t.trace_id or None,
                        agent_id=exp.agent_id,
                        tier=2,
                        source="native",
                        duration_ms=t.duration_ms(),
                        status=t.status,
                        input={"question": t.question},
                        output={"answer": exp.answer},
                        started_at=t.started_at or 0.0,
                        completed_at=t.completed_at or 0.0,
                        performance={
                            "reasoning_len": exp.reasoning_len,
                            "trajectory_steps": exp.trajectory_steps,
                            "tool_count": len(exp.tools),
                        },
                    )
                )
            if not t.experts:
                # No nested expert: record the turn itself as a tier-1 invocation.
                out.append(
                    Invocation(
                        trace_id=t.trace_id or t.turn_id,
                        session_id=session_id,
                        parent_trace_id=None,
                        agent_id=t.selected_expert or "orchestrator",
                        tier=1,
                        source="native",
                        duration_ms=t.duration_ms(),
                        status=t.status,
                        input={"question": t.question},
                        output={"answer": t.answer} if not t.error else {"error": t.error},
                        started_at=t.started_at or 0.0,
                        completed_at=t.completed_at or 0.0,
                    )
                )
        return out


def _apply(turn: _LiveTurn, content: dict[str, Any]) -> None:
    """Replay ONE persisted ``semantic_event`` content dict into the per-turn aggregate.

    This is the SAME aggregation the post-hoc fold did, at read-time over the ONE log:
    it reads the EVENT CONTENT fields (``event_type`` / ``payload`` / ``actor`` /
    ``provider`` / ``status`` / ``occurred_at``) -- the identical extraction the old
    fold performed from the raw event -- so the projections it feeds are byte-identical
    to the historical post-hoc recorder's. It is the single reducer over a turn's
    events, shared by every read.
    """
    etype = str(content.get("event_type") or "")
    occurred = _epoch(str(content.get("occurred_at") or ""))
    trace_id = str(content.get("trace_id") or "")
    if trace_id and not turn.trace_id:
        turn.trace_id = trace_id
    payload = content.get("payload") or {}
    actor = content.get("actor") or {}

    if etype == "turn.started":
        turn.started_at = occurred or turn.started_at
        question = str(payload.get("input") or payload.get("question") or "")
        turn.question = question or turn.question
    elif etype == "llm.request.started":
        if not turn.question:
            turn.question = str(payload.get("input") or "")
    elif etype == "llm.response.completed":
        turn.selected_expert = str(payload.get("selected_expert") or "") or turn.selected_expert
        turn.route_reason = str(payload.get("route_reason") or "") or turn.route_reason
        if payload.get("answer"):
            turn.answer = str(payload.get("answer") or "")
    elif etype == "expert.response.completed":
        agent_id = str(actor.get("agent_id") or "") or "unknown"
        exp = turn.experts.get(agent_id) or _LiveExpert(agent_id=agent_id)
        # Mirror the original fold's per-field overwrite semantics EXACTLY.
        exp.answer = str(payload.get("answer") or "") or exp.answer
        exp.reasoning_len = len(str(payload.get("reasoning") or "")) or exp.reasoning_len
        # Only overwrite trajectory_steps / tools when the event actually supplied
        # them (a dict / a list), matching the original "overwrite only when present".
        traj = payload.get("trajectory")
        if isinstance(traj, dict):
            exp.trajectory_steps = sum(1 for k in traj if str(k).startswith("tool_name_"))
        tools = payload.get("tools_called")
        if isinstance(tools, list):
            exp.tools = [_lean_tool(t) for t in tools]
        provider = content.get("provider")
        if isinstance(provider, dict) and provider:
            exp.provider = provider
        turn.experts[agent_id] = exp
    elif etype == "tool.call.completed":
        tool = str(actor.get("tool") or payload.get("tool") or "")
        turn.tools.append({"tool": tool, "status": str(content.get("status") or "")})
    elif etype in ("turn.completed", "turn.failed"):
        turn.completed_at = occurred or turn.completed_at
        turn.status = "success" if etype == "turn.completed" else "failure"
        final = payload.get("final_message")
        if isinstance(final, dict):
            answer = _message_text(final)
            if answer:
                turn.answer = answer
        if etype == "turn.failed":
            err = payload.get("error_info") or {}
            turn.error = str((err.get("error") if isinstance(err, dict) else err) or "")


def _lean_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {"name": str(tool)}
    return {
        "name": str(tool.get("name") or ""),
        "ok": bool(tool.get("ok", True)),
    }


def _message_text(message: dict[str, Any]) -> str:
    """Extract the assistant text from a serialized gact Message."""
    parts = message.get("parts")
    if isinstance(parts, list):
        texts = [
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    return str(message.get("content") or "")
