"""Live runtime context: fold semantic events into ARC state in real time.

The canonical semantic-event trace is the source of truth for "what happened"
in a turn. Historically ARC rebuilt Invocation/Conversation records *post hoc*
from dspy Predictions and run traces -- a second, divergent recorder. This
module folds the SAME events the durable trace captures into a live, per-session
runtime context, so ARC's Invocation/Conversation become **projections** of the
trace instead of independent builds.

Design:
    * Fed the RAW, unredacted ``SemanticEvent`` via ``LiveRuntimeContext.fold``
      (registered as a sink ``live_consumer``).
    * LEAN by construction: only the fields needed to project records + compile
      context are kept, and text is capped -- the full payloads live in the
      durable trace, not this hot in-memory copy.
    * Released per session (``release``) and wholesale (``clear``) by ARC's
      lifecycle so an idle server returns to baseline.

``project_conversation`` / ``project_invocations`` produce valid
``clio_agent.arc.schema`` objects from the fold; ``view`` is the compact summary
``context_compiler`` reads for the open session.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from clio_agent.arc.schema import Conversation, Invocation, Message

# Bound the hot copy: cap retained text and turns per session. The full,
# uncapped content is always available in the durable trace.
_MAX_TEXT = 4096
_MAX_TURNS_PER_SESSION = 50


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
    """Lean fold of one turn's events."""

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


class LiveRuntimeContext:
    """Thread-safe live fold of semantic events, keyed by session -> turn."""

    def __init__(self) -> None:
        self._sessions: dict[str, OrderedDict[str, _LiveTurn]] = {}
        self._lock = threading.Lock()

    # ---- ingest --------------------------------------------------------

    def fold(self, event: Any) -> None:
        """Fold one RAW SemanticEvent into the live context. Best-effort."""
        try:
            self._fold(event)
        except Exception:  # noqa: BLE001 - a live fold must never break a turn
            pass

    def _turn(self, sid: str, turn_id: str, trace_id: str) -> _LiveTurn:
        turns = self._sessions.setdefault(sid, OrderedDict())
        turn = turns.get(turn_id)
        if turn is None:
            turn = _LiveTurn(turn_id=turn_id, trace_id=trace_id)
            turns[turn_id] = turn
            # Evict oldest turns to stay lean (durable trace retains all).
            while len(turns) > _MAX_TURNS_PER_SESSION:
                turns.popitem(last=False)
        elif trace_id and not turn.trace_id:
            turn.trace_id = trace_id
        return turn

    def _fold(self, event: Any) -> None:
        etype = str(getattr(event, "event_type", "") or "")
        sid = str(getattr(event, "session_id", "") or "")
        turn_id = str(getattr(event, "turn_id", "") or "") or "_no_turn"
        if not sid:
            return
        trace_id = str(getattr(event, "trace_id", "") or "")
        occurred = _epoch(str(getattr(event, "occurred_at", "") or ""))
        actor = getattr(event, "actor", {}) or {}
        payload = getattr(event, "payload", {}) or {}

        with self._lock:
            turn = self._turn(sid, turn_id, trace_id)

            if etype == "turn.started":
                turn.started_at = occurred or turn.started_at
                turn.question = _cap(
                    payload.get("input") or payload.get("question") or turn.question
                )
            elif etype == "llm.request.started":
                if not turn.question:
                    turn.question = _cap(payload.get("input") or "")
            elif etype == "llm.response.completed":
                turn.selected_expert = (
                    str(payload.get("selected_expert") or "") or turn.selected_expert
                )
                turn.route_reason = str(payload.get("route_reason") or "") or turn.route_reason
                if payload.get("answer"):
                    turn.answer = _cap(payload.get("answer"))
            elif etype == "expert.response.completed":
                agent_id = str(actor.get("agent_id") or "") or "unknown"
                exp = turn.experts.get(agent_id) or _LiveExpert(agent_id=agent_id)
                exp.answer = _cap(payload.get("answer") or exp.answer)
                exp.reasoning_len = len(str(payload.get("reasoning") or "")) or exp.reasoning_len
                traj = payload.get("trajectory")
                if isinstance(traj, dict):
                    exp.trajectory_steps = sum(1 for k in traj if str(k).startswith("tool_name_"))
                tools = payload.get("tools_called")
                if isinstance(tools, list):
                    exp.tools = [_lean_tool(t) for t in tools]
                exp.provider = getattr(event, "provider", {}) or exp.provider
                turn.experts[agent_id] = exp
            elif etype == "tool.call.completed":
                turn.tools.append(
                    {
                        "tool": str(actor.get("tool") or payload.get("tool") or ""),
                        "status": str(getattr(event, "status", "") or ""),
                    }
                )
            elif etype in ("turn.completed", "turn.failed"):
                turn.completed_at = occurred or turn.completed_at
                turn.status = "success" if etype == "turn.completed" else "failure"
                final = payload.get("final_message")
                if isinstance(final, dict):
                    turn.answer = _cap(_message_text(final)) or turn.answer
                if etype == "turn.failed":
                    err = payload.get("error_info") or {}
                    turn.error = _cap(err.get("error") if isinstance(err, dict) else err)

    # ---- lifecycle -----------------------------------------------------

    def release(self, session_id: str) -> int:
        """Drop a session's live turns. Returns count released."""
        with self._lock:
            turns = self._sessions.pop(session_id, None)
            return len(turns) if turns else 0

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    # ---- read / project ------------------------------------------------

    def view(self, session_id: str, *, max_turns: int = 5) -> dict[str, Any]:
        """Compact summary of a session's recent turns (for context_compiler)."""
        with self._lock:
            turns = self._sessions.get(session_id)
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
        with self._lock:
            turns = self._sessions.get(session_id)
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
        with self._lock:
            turns = self._sessions.get(session_id)
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
