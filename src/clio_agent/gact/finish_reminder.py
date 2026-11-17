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

__all__ = ["FINISH_WORK_SOURCE", "finish_work_reminder"]

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
