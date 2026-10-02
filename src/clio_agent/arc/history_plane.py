"""The History mode plane: each agent scope's context in a DSPy ``History``, in memory.

Only in the loud History mode (:mod:`clio_agent.arc.history_mode`, the platform has no
clio-core). It holds the same segment records clio-core would, one immutable
``dspy.History`` per ``(session, scope)``, and answers the plane surface the loop and the
variant lines use (append, list, render, working set, delete), so the one projection
(``clio_react_record.fold_steps``) serves both modes. Nothing is durable: after a restart
a scope is empty and the loop seeds it from the transcript file (``carry_over``), as it
does for any scope new to a conversation. Undo/rewind drops the session's scopes (the
next turn reseeds them from the rolled-back transcript); context edits, compaction and
search answer ``history_mode_unsupported``.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

import dspy
import msgspec

from clio_agent.arc import history_mode
from clio_agent.arc.context_view import ViewSnapshot, next_generation
from clio_agent.arc.schema import WORKING_SET_KINDS, Segment


class HistoryPlane:
    """One process's in-memory agent contexts, a ``dspy.History`` per agent scope."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scopes: dict[tuple[str, str], dspy.History] = {}
        self._clock = itertools.count(1)
        # Renewed by every delete / drop, so a reader never reuses a stale prefix.
        self._generations: dict[tuple[str, str], int] = {}

    def history(self, session_id: str, scope: str) -> dspy.History:
        """The scope's record as it stands (segment records, in write order)."""
        with self._lock:
            return self._scopes.get((session_id, scope), dspy.History(messages=[]))

    def append_segment(
        self,
        session_id: str,
        scope: str,
        kind: str,
        content: dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment:
        """Append one segment at the end of the scope."""
        with self._lock:
            key = (session_id, scope)
            records = self._scopes.get(key, dspy.History(messages=[])).messages
            tick = next(self._clock)
            seg = Segment(
                scope=scope,
                kind=kind,  # type: ignore[arg-type]
                content=dict(content),
                session_id=session_id,
                step=step,
                order=float(len(records) + 1),
                logical_time=tick,
                token_count=token_count,
                trace_ref=trace_ref,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            self._scopes[key] = dspy.History(messages=[*records, msgspec.to_builtins(seg)])
            return seg

    def list_segments(
        self, session_id: str, scope: str, *, include_tombstoned: bool = False
    ) -> list[Segment]:
        """The scope's segments in order; ``include_tombstoned`` keeps deleted ones."""
        segs = [msgspec.convert(r, Segment) for r in self.history(session_id, scope).messages]
        return segs if include_tombstoned else [s for s in segs if s.status == "live"]

    def render_segments(self, session_id: str, scope: str) -> list[Segment]:
        """The scope's live segments in order."""
        return self.list_segments(session_id, scope)

    def context_view(self, session_id: str, scope: str) -> ViewSnapshot:
        """The scope's live segments and their generation (appends keep it)."""
        with self._lock:
            key = (session_id, scope)
            generation = self._generations.get(key, 0)
            records = self._scopes.get(key, dspy.History(messages=[])).messages
        live = [msgspec.convert(r, Segment) for r in records if r["status"] == "live"]
        return ViewSnapshot(generation, tuple(live))

    def has_segments(self, session_id: str, scope: str) -> bool:
        """Whether anything was ever recorded in the scope (any status)."""
        return bool(self.history(session_id, scope).messages)

    def render_working_set(self, session_id: str, scope: str) -> list[Segment]:
        """The live segments the agent's context is folded from."""
        return [s for s in self.render_segments(session_id, scope) if s.kind in WORKING_SET_KINDS]

    def delete_segments(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Tombstone segments by id (kept in the record, skipped by render)."""
        wanted = set(ids)
        with self._lock:
            key = (session_id, scope)
            records = self._scopes.get(key, dspy.History(messages=[])).messages
            tick = next(self._clock)
            out: list[dict[str, Any]] = []
            deleted = 0
            for rec in records:
                if rec["id"] in wanted and rec["status"] == "live":
                    rec = {**rec, "status": "tombstoned", "tombstoned_at": tick}
                    deleted += 1
                out.append(rec)
            self._scopes[key] = dspy.History(messages=out)
            self._generations[key] = next_generation()
            return deleted

    def drop_session(self, session_id: str) -> None:
        """Forget every scope of a session (undo/rewind): the next turn reseeds each
        scope from the rolled-back transcript."""
        with self._lock:
            for key in [k for k in self._scopes if k[0] == session_id]:
                del self._scopes[key]
                self._generations[key] = next_generation()


def plane_for(app: Any) -> Any:
    """The app's context plane: its clio-core ARC, else (History mode only) its
    :class:`HistoryPlane`, else ``None``."""
    state = getattr(app, "state", None)
    arc = getattr(state, "arc", None)
    if arc is not None or state is None or not history_mode.active():
        return arc
    plane = getattr(state, "history_plane", None)
    if plane is None:
        plane = HistoryPlane()
        state.history_plane = plane
    return plane
