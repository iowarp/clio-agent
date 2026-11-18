"""The turn-start reminder to carry a request through (``clio.reminder.finish_work``).

Some models answer with a list of what they did not do. The core guideline
``core.finish`` says otherwise in the system prompt; in live runs that alone did not
change the habit. This reminder rides ahead of the person's message at the start of
each turn of a root session, only for the providers it names (``x_param_providers``,
edited with the reminder). It decides nothing: it is text the agent reads.
"""

from __future__ import annotations

from typing import Any

from clio_agent import injections

__all__ = [
    "FINISH_CHECK_SOURCE",
    "FINISH_WORK_SOURCE",
    "finish_work_reminder",
    "note_finish_check",
]

#: The injection's source (the transcript shows it as harness data).
FINISH_WORK_SOURCE = "finish_work"


def _providers(app: Any, session_id: str) -> set[str]:
    raw = injections.param(FINISH_WORK_SOURCE, "providers", "", str, app=app, session_id=session_id)
    return {item.strip() for item in raw.split(",") if item.strip()}


def finish_work_reminder(app: Any, session_id: str) -> str:
    """The reminder's text for this turn, or ``""`` when it does not apply or is off."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        session_is_delegated,
        session_provider_id,
    )

    if session_is_delegated(app, session_id):
        return ""
    if session_provider_id(app, session_id) not in _providers(app, session_id):
        return ""
    return injections.render(FINISH_WORK_SOURCE, {}, app=app, session_id=session_id) or ""


#: The mid-turn repeat (``clio.reminder.finish_check``): the turn-start text fades over a
#: long turn, so it is said once more after a run of calls, nearer the answer.
FINISH_CHECK_SOURCE = "finish_check"
_DEFAULT_CHECK_AFTER = 10


def _check_applies(loop: Any) -> bool:
    cached = getattr(loop, "_clio_finish_check_applies", None)
    if cached is not None:
        return cached
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        session_is_delegated,
        session_provider_id,
    )

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    raw = injections.param(FINISH_CHECK_SOURCE, "providers", "", str)
    providers = {item.strip() for item in raw.split(",") if item.strip()}
    applies = bool(session_id) and not session_is_delegated(app, session_id)
    applies = applies and session_provider_id(app, session_id) in providers
    loop._clio_finish_check_applies = applies
    return applies


def note_finish_check(loop: Any, call_names: list[str]) -> None:
    """Count a step's calls; past the threshold, repeat the finish reminder once per turn."""
    if getattr(loop, "_clio_finish_checked", False) or not call_names:
        return
    count = getattr(loop, "_clio_finish_calls", 0) + len(call_names)
    loop._clio_finish_calls = count
    after = getattr(loop, "_clio_finish_after", None)
    if after is None:  # read once per turn: the loop lives for one turn
        after = loop._clio_finish_after = injections.param(
            FINISH_CHECK_SOURCE, "after", _DEFAULT_CHECK_AFTER, int
        )
    if after <= 0 or count < after or not _check_applies(loop):
        return
    loop._clio_finish_checked = True
    text = injections.render(FINISH_CHECK_SOURCE, {"calls": count})
    if text:  # an event note: recorded even when an earlier turn's note had these words
        loop.recorder.injections([(FINISH_CHECK_SOURCE, injections.fired(text))])
