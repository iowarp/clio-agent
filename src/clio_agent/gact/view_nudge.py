"""A note, not a decision: after a run of calls with nothing shown, say that views exist.

The core guideline ``core.visual`` asks the agent to show results with a shape in a view
and look at them. In live runs that text, read at the start of a turn, faded: an agent
deep in shell work decided late to "skip plotting". After a number of calls in one turn
of a root session with no view created, changed or captured, the loop adds one
injection saying so. Nothing is drawn and nothing is required: the agent decides, the
note comes at most once per turn, and an agent that cannot create and capture views is
never told.

The note is the reminder ``clio.reminder.view_nudge``
(``plugins/builtin/core/ai.iowarp.clio/injections/view_nudge.md``): its switch, wording
and threshold (``x_param_after``; 0 never notes) are edited on the Reminders page.
"""

from __future__ import annotations

from typing import Any

from clio_agent import injections

__all__ = ["VIEW_NUDGE_SOURCE", "VIEW_TOOLS", "note_call_run"]

#: The injection's source (the transcript shows it as harness data).
VIEW_NUDGE_SOURCE = "view_nudge"
#: Calls that show or look at a view; one restarts the count.
VIEW_TOOLS = frozenset(
    {"create_a2ui_surface", "update_a2ui_components", "capture_a2ui_surface", "view_image"}
)
_NEEDED = ("create_a2ui_surface", "capture_a2ui_surface")
_DEFAULT_AFTER = 5


def _after() -> int:
    return injections.param(VIEW_NUDGE_SOURCE, "after", _DEFAULT_AFTER, int)


def _applies(loop: Any) -> bool:
    """Whether this loop's agent can make and look at views, in a root session."""
    cached = getattr(loop, "_clio_view_nudge_applies", None)
    if cached is not None:
        return cached
    tools = set(getattr(loop.agent, "tools", {}) or {})
    applies = all(name in tools for name in _NEEDED)
    if applies:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.agents.resolution import session_is_delegated  # noqa: PLC0415

        app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
        applies = bool(session_id) and not session_is_delegated(app, session_id)
    loop._clio_view_nudge_applies = applies
    return applies


def note_call_run(loop: Any, call_names: list[str]) -> None:
    """Count a step's calls; past the threshold with no view, note it once per turn."""
    if getattr(loop, "_clio_view_noted", False) or not call_names:
        return
    if VIEW_TOOLS & set(call_names):
        loop._clio_view_calls = 0
        return
    count = getattr(loop, "_clio_view_calls", 0) + len(call_names)
    loop._clio_view_calls = count
    after = getattr(loop, "_clio_view_after", None)
    if after is None:  # read once per turn: the loop lives for one turn
        after = loop._clio_view_after = _after()
    if after <= 0 or count < after or not _applies(loop):
        return
    loop._clio_view_noted = True
    text = injections.render(VIEW_NUDGE_SOURCE, {"calls": count})
    if text:  # an event note: recorded even when an earlier turn's note had these words
        loop.recorder.injections([(VIEW_NUDGE_SOURCE, injections.fired(text))])
