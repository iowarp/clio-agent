"""Live runtime context: fold semantic events into ARC's ONE segment buffer.

The canonical semantic-event trace is the source of truth for "what happened"
in a turn. Historically ARC rebuilt Invocation/Conversation records *post hoc*
from dspy Predictions and run traces -- a second, divergent recorder. This
module folds the SAME events the durable trace captures into the live segment
buffer, so ARC's Invocation/Conversation become **projections** of the trace
instead of independent builds.

Design (ARC unification Q3):
    * There is NO separate ``self._sessions`` dict. The observer's state lives in
      the ONE :class:`~clio_agent.arc.segments.SegmentStore`, in a reserved
      ``_live`` scope, as append-only ``turn_event`` segments. Every fold is a
      single ``append`` (no read-modify-write churn).
    * ``fold(event)`` is fed the RAW, unredacted ``SemanticEvent`` (registered as
      a sink ``live_consumer``). For each handled event type it appends ONE lean
      ``turn_event`` segment correlated by ``turn_id`` / ``expert_span_id`` --
      only the fields needed to project records, with text capped, since the full
      payloads live in the durable trace.
    * The reads (``view`` / ``project_conversation`` / ``project_invocations``)
      are QUERIES over the buffer: ``render`` the ``_live`` scope, GROUP by
      ``turn_id`` (first-seen order), REPLAY each turn's events in logical_time
      order to rebuild the lean per-turn fold, then run the SAME projection
      construction as the post-hoc recorder did. The per-turn fold logic is the
      single ``_apply`` reducer below, shared by every read.
    * The ``_live`` scope is INVISIBLE to the expert-prompt render: it is its own
      scope, so a working-set render of an expert scope never sees it, and
      ``turn_event`` is not a working-set kind nor part of the dspy trajectory
      projection.
    * Released per session (``release``) and wholesale (``clear``) -- the ``_live``
      scope's segments are ERASED (``drop_scope``) so an idle server returns to
      baseline.

``project_conversation`` / ``project_invocations`` produce valid
``clio_agent.arc.schema`` objects from the buffer; ``view`` is the compact summary
``context_compiler`` reads for the open session.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

from clio_agent.arc.schema import Conversation, Invocation, Message
from clio_agent.arc.segments import SegmentStore

# Reserved scope that holds the live-observer's folded event segments. It is its
# OWN scope, so an expert/working-set render (which renders a specific expert
# scope) never sees it; combined with ``turn_event`` not being a working-set kind,
# the live observer's state can never leak into a model prompt.
LIVE_SCOPE = "_live"

# Bound the hot copy: cap retained text and turns per session. The full,
# uncapped content is always available in the durable trace.
_MAX_TEXT = 4096
_MAX_TURNS_PER_SESSION = 50

_NO_TURN = "_no_turn"


def _cap(text: Any) -> str:
    s = str(text or "")
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + f"...[+{len(s) - _MAX_TEXT} chars]"


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
    """Lean fold of one turn's events (rebuilt at read-time by replaying the
    turn's ``turn_event`` segments through :func:`_apply`)."""

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

    def put(self, kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: Any = None) -> None:
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

    def search(self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10) -> list[tuple[str, float]]:
        return []


class LiveRuntimeContext:
    """Live fold of semantic events, backed by the ONE SegmentStore.

    The observer's per-session state IS the ``_live`` scope of the injected
    :class:`~clio_agent.arc.segments.SegmentStore`: ``fold`` appends one lean
    ``turn_event`` segment per event, and the reads query+group+replay that scope.
    Thread-safety is inherited from the SegmentStore's per-scope locking.
    """

    def __init__(self, store: SegmentStore | None = None) -> None:
        """Back the observer with a SegmentStore.

        Args:
            store: The SegmentStore the observer reads/writes its ``_live`` scope
                in. ARCMemory passes ``self._segments`` so the observer rides the
                same buffer (and highway op log) as the live context plane. When
                ``None`` (standalone / unit tests), a private in-memory-backed
                SegmentStore is created so the observer is self-contained.
        """
        if store is None:
            store = SegmentStore(_MemoryStore())
        self._segments = store

    # ---- ingest --------------------------------------------------------

    def fold(self, event: Any) -> None:
        """Fold one RAW SemanticEvent into the buffer as a ``turn_event`` segment.

        Append-only: one segment per handled event, correlated by ``turn_id`` /
        ``expert_span_id``. Best-effort -- a live fold must never break a turn.
        """
        try:
            self._fold(event)
        except Exception:  # noqa: BLE001 - a live fold must never break a turn
            pass

    def _fold(self, event: Any) -> None:
        sid = str(getattr(event, "session_id", "") or "")
        if not sid:
            return
        content = self._event_content(event)
        if content is None:  # an event type we don't fold -> nothing to buffer
            return
        turn_id = str(getattr(event, "turn_id", "") or "") or _NO_TURN
        expert_span_id = str(getattr(event, "expert_span_id", "") or "") or str(
            getattr(event, "span_id", "") or ""
        )
        self._segments.append(
            sid,
            LIVE_SCOPE,
            "turn_event",
            content,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
        )

    @staticmethod
    def _event_content(event: Any) -> Optional[dict[str, Any]]:
        """The lean per-event fields to buffer, or ``None`` for an unfolded type.

        These are exactly the fields the post-hoc fold extracted; they are replayed
        by :func:`_apply` at read-time to rebuild the per-turn aggregate."""
        etype = str(getattr(event, "event_type", "") or "")
        trace_id = str(getattr(event, "trace_id", "") or "")
        occurred = _epoch(str(getattr(event, "occurred_at", "") or ""))
        actor = getattr(event, "actor", {}) or {}
        payload = getattr(event, "payload", {}) or {}

        base: dict[str, Any] = {"etype": etype, "trace_id": trace_id, "occurred": occurred}

        if etype == "turn.started":
            base["question"] = _cap(payload.get("input") or payload.get("question") or "")
            return base
        if etype == "llm.request.started":
            base["question"] = _cap(payload.get("input") or "")
            return base
        if etype == "llm.response.completed":
            base["selected_expert"] = str(payload.get("selected_expert") or "")
            base["route_reason"] = str(payload.get("route_reason") or "")
            base["answer"] = _cap(payload.get("answer")) if payload.get("answer") else ""
            return base
        if etype == "expert.response.completed":
            agent_id = str(actor.get("agent_id") or "") or "unknown"
            base["agent_id"] = agent_id
            base["answer"] = _cap(payload.get("answer") or "")
            base["reasoning_len"] = len(str(payload.get("reasoning") or ""))
            # Only carry trajectory_steps / tools when the event actually supplied
            # them (a dict / a list), so the read-time reducer matches the original
            # fold's "overwrite only when present" semantics exactly.
            traj = payload.get("trajectory")
            if isinstance(traj, dict):
                base["trajectory_steps"] = sum(
                    1 for k in traj if str(k).startswith("tool_name_")
                )
            tools = payload.get("tools_called")
            if isinstance(tools, list):
                base["tools"] = [_lean_tool(t) for t in tools]
            base["provider"] = getattr(event, "provider", {}) or {}
            return base
        if etype == "tool.call.completed":
            base["tool"] = str(actor.get("tool") or payload.get("tool") or "")
            base["status"] = str(getattr(event, "status", "") or "")
            return base
        if etype in ("turn.completed", "turn.failed"):
            base["status"] = "success" if etype == "turn.completed" else "failure"
            final = payload.get("final_message")
            base["answer"] = _cap(_message_text(final)) if isinstance(final, dict) else ""
            if etype == "turn.failed":
                err = payload.get("error_info") or {}
                base["error"] = _cap(err.get("error") if isinstance(err, dict) else err)
            else:
                base["error"] = ""
            return base
        return None

    # ---- lifecycle -----------------------------------------------------

    def release(self, session_id: str) -> int:
        """Drop a session's live turns (erase the ``_live`` scope). Returns the
        number of turns released (NOT segments), matching the historical contract."""
        turn_count = len(self._turns(session_id))
        self._segments.drop_scope(session_id, LIVE_SCOPE)
        return turn_count

    def clear(self) -> None:
        """Erase the ``_live`` scope across all sessions (idle -> baseline)."""
        for session_id in self._live_session_ids():
            self._segments.drop_scope(session_id, LIVE_SCOPE)

    def _live_session_ids(self) -> list[str]:
        """Every session that currently holds a ``_live`` scope record (so ``clear``
        can erase them all). The ``_live`` scope record name is ``<sid>__<_live>``."""
        suffix = SegmentStore._record_name("", LIVE_SCOPE)  # "__" + the live scope
        sessions: set[str] = set()
        for name, _ in self._segments._store.scan("segments"):
            if name.endswith(suffix):
                sessions.add(name[: -len(suffix)])
        return sorted(sessions)

    # ---- read / replay -------------------------------------------------

    def _turns(self, session_id: str) -> "OrderedDict[str, _LiveTurn]":
        """Rebuild the per-session turns by querying the ``_live`` scope, GROUPING the
        ``turn_event`` segments by ``turn_id`` (first-seen order), and REPLAYING each
        turn's events (in render = (order, logical_time) order, which is fold order)
        through :func:`_apply`. The last ``_MAX_TURNS_PER_SESSION`` turn_ids are kept."""
        segments = self._segments.render(session_id, LIVE_SCOPE)
        grouped: "OrderedDict[str, list[Any]]" = OrderedDict()
        for seg in segments:
            grouped.setdefault(seg.turn_id or _NO_TURN, []).append(seg)
        # Apply the per-session turn cap at read-time (last N first-seen turn_ids).
        turn_ids = list(grouped.keys())
        if len(turn_ids) > _MAX_TURNS_PER_SESSION:
            turn_ids = turn_ids[-_MAX_TURNS_PER_SESSION:]
        turns: "OrderedDict[str, _LiveTurn]" = OrderedDict()
        for turn_id in turn_ids:
            turn = _LiveTurn(turn_id=turn_id)
            for seg in grouped[turn_id]:
                _apply(turn, seg.content)
            turns[turn_id] = turn
        return turns

    def view(self, session_id: str, *, max_turns: int = 5) -> dict[str, Any]:
        """Compact summary of a session's recent turns (for context_compiler)."""
        turns = self._turns(session_id)
        if not turns:
            return {}
        recent = list(turns.values())[-max_turns:]
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
    """Replay ONE buffered ``turn_event`` content dict into the per-turn aggregate.

    This is the SAME aggregation the post-hoc fold did, moved to read-time: it is
    the single reducer over a turn's events, so the projections it feeds are
    byte-identical to the historical post-hoc recorder's.
    """
    etype = str(content.get("etype") or "")
    occurred = float(content.get("occurred") or 0.0)
    trace_id = str(content.get("trace_id") or "")
    if trace_id and not turn.trace_id:
        turn.trace_id = trace_id

    if etype == "turn.started":
        turn.started_at = occurred or turn.started_at
        turn.question = str(content.get("question") or "") or turn.question
    elif etype == "llm.request.started":
        if not turn.question:
            turn.question = str(content.get("question") or "")
    elif etype == "llm.response.completed":
        turn.selected_expert = str(content.get("selected_expert") or "") or turn.selected_expert
        turn.route_reason = str(content.get("route_reason") or "") or turn.route_reason
        if content.get("answer"):
            turn.answer = str(content.get("answer"))
    elif etype == "expert.response.completed":
        agent_id = str(content.get("agent_id") or "") or "unknown"
        exp = turn.experts.get(agent_id) or _LiveExpert(agent_id=agent_id)
        # Mirror the original fold's per-field overwrite semantics EXACTLY.
        exp.answer = str(content.get("answer") or "") or exp.answer
        exp.reasoning_len = int(content.get("reasoning_len") or 0) or exp.reasoning_len
        if "trajectory_steps" in content:  # event supplied a trajectory dict
            exp.trajectory_steps = int(content["trajectory_steps"])
        if "tools" in content:  # event supplied a tools list (even if empty)
            exp.tools = list(content["tools"])
        provider = content.get("provider")
        if isinstance(provider, dict) and provider:
            exp.provider = provider
        turn.experts[agent_id] = exp
    elif etype == "tool.call.completed":
        turn.tools.append(
            {"tool": str(content.get("tool") or ""), "status": str(content.get("status") or "")}
        )
    elif etype in ("turn.completed", "turn.failed"):
        turn.completed_at = occurred or turn.completed_at
        turn.status = str(content.get("status") or turn.status)
        if content.get("answer"):
            turn.answer = str(content.get("answer")) or turn.answer
        if content.get("error"):
            turn.error = str(content.get("error"))


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
