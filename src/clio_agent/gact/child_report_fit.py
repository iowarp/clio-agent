"""A child's report reaches its parent whole, unless it cannot fit the parent's context.

A parent starts a child to get its report, so CLIO does not decide how much of it the
parent may read: there is no per-report share and no digest. The one limit is real: the
parent's context window. The room is measured once per call that collects reports
(:func:`report_room`, on the caller's context, where the loop's window and its last
prompt size are known) as the window up to the auto-compaction threshold, less the
prompt the parent already carries. Each report delivered whole spends from that room;
a report that does not fit is saved to a file the parent can read
(:mod:`clio_agent.gact.task_result_spill`), traced as :data:`REPORT_EXCEEDS_CONTEXT`.

When the window is unknown there is no limit to gate on: the report is delivered whole
and the miss is traced as :data:`CONTEXT_WINDOW_UNKNOWN`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from clio_agent.runtime import trace

__all__ = [
    "CHILD_REPORT_SOURCE",
    "CONTEXT_WINDOW_UNKNOWN",
    "REPORT_EXCEEDS_CONTEXT",
    "child_report_note",
    "report_fits",
    "report_room",
]

#: The reminder a child reads about its own answer (``clio.reminder.child_report``).
CHILD_REPORT_SOURCE = "child_report"

#: Typed reason: a report was saved to a file because it does not fit the parent's room.
REPORT_EXCEEDS_CONTEXT = "report_exceeds_context"
#: Typed reason: the parent's window is unknown, so reports are delivered whole.
CONTEXT_WINDOW_UNKNOWN = "context_window_unknown"


@dataclass
class _Room:
    """Tokens left for reports in one collecting call (``None``: window unknown).

    Mutable on purpose: Core I/O runs collection in a copied context, and the copy
    shares this object, so every report of one call spends from the same room.
    """

    tokens: int | None


_ROOM: ContextVar[_Room | None] = ContextVar("clio_child_report_room", default=None)


def _measure() -> _Room:
    from clio_agent.gact.context import active_react_context_window  # noqa: PLC0415
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _autocompact_threshold,
        _last_prompt_tokens,
    )

    window = active_react_context_window()
    if window <= 0:
        trace.event("TOOLS", "%s: reports are delivered whole", CONTEXT_WINDOW_UNKNOWN)
        return _Room(tokens=None)
    return _Room(tokens=int(window * _autocompact_threshold()) - _last_prompt_tokens())


@contextmanager
def report_room() -> Iterator[None]:
    """Measure the parent's room once for the reports one call collects."""
    token = _ROOM.set(_measure())
    try:
        yield
    finally:
        _ROOM.reset(token)


def report_fits(encoded: str) -> bool:
    """Whether ``encoded`` fits the room left; a report that fits spends from it."""
    from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens  # noqa: PLC0415

    room = _ROOM.get() or _measure()
    if room.tokens is None:
        return True
    need = _estimate_text_tokens(encoded)
    if need <= room.tokens:
        room.tokens -= need
        return True
    trace.event("TOOLS", "%s tokens=%d room=%d", REPORT_EXCEEDS_CONTEXT, need, max(room.tokens, 0))
    return False


def child_report_note(app: object, session_id: str, session: object) -> str:
    """For a session another agent started: who reads its answer (``""`` otherwise).

    A fact, not a limit: the child decides how long its answer must be.
    """
    if not str(getattr(session, "parent_session_id", "") or ""):
        return ""
    from clio_agent import injections  # noqa: PLC0415

    return injections.render(CHILD_REPORT_SOURCE, {}, app=app, session_id=session_id) or ""
