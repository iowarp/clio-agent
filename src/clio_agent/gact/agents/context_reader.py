"""The loop's context read: the scope's view folded into messages, the closed steps kept.

Every ReAct step reads its context back from the ARC live plane
(:func:`~clio_agent.gact.agents.clio_react_record.fold_steps` over the scope's
context view). Within one view generation the plane only grows at the end, so the
messages of every step that a later ``thought`` / ``user`` / ``summary`` closed can not
change: :class:`ContextReader` keeps them and re-folds only the open tail step. A new
generation (a recorded op: compaction, delete, rollback) folds from the start again.

Folding is split only at those boundary segments, where :func:`fold_steps` closes the
open step and starts afresh, so ``fold(segs[:b]) + fold(segs[b:]) == fold(segs)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dspy.lm15 import Message

__all__ = ["ContextReader"]

# Segment kinds that close the open step and start a new message group in fold_steps.
_BOUNDARY_KINDS = frozenset({"thought", "user", "summary"})


class ContextReader:
    """Reads one (session, scope)'s context as messages, caching the closed steps."""

    def __init__(self, arc: Any, session: str, scope: str) -> None:
        """Bind the reader to a context plane and scope.

        Args:
            arc: The plane (``ARCMemory`` or the History-mode ``HistoryPlane``); it must
                answer ``context_view(session, scope)``.
            session: The session id.
            scope: The run-keyed agent scope.
        """
        self.arc = arc
        self.session = session
        self.scope = scope
        self._generation: int | None = None
        self._closed_upto = 0  # segments [0, n) are folded into ``_closed``
        self._closed: list[Message] = []

    def read(self) -> list[Message]:
        """The scope's context now, as typed messages.

        Raises:
            ContextReadError: The plane could not be read.
            ContextFoldError: A segment cannot be folded exactly.
        """
        from clio_agent.gact.agents.clio_react_record import (  # noqa: PLC0415
            ContextReadError,
            fold_steps,
        )

        try:
            snapshot = self.arc.context_view(self.session, self.scope)
        except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
            raise ContextReadError(self.scope, exc) from exc
        segments = snapshot.segments
        if snapshot.generation != self._generation or len(segments) < self._closed_upto:
            self._generation = snapshot.generation
            self._closed_upto = 0
            self._closed = []
        boundary = _last_boundary(segments, self._closed_upto)
        if boundary > self._closed_upto:
            self._closed.extend(fold_steps(segments[self._closed_upto : boundary]))
            self._closed_upto = boundary
        return [*self._closed, *fold_steps(segments[self._closed_upto :])]


def _last_boundary(segments: Sequence[Any], start: int) -> int:
    """Index of the last boundary segment at or after ``start`` (``start`` if none)."""
    for index in range(len(segments) - 1, start, -1):
        if getattr(segments[index], "kind", "") in _BOUNDARY_KINDS:
            return index
    return start
