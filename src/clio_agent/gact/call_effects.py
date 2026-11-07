"""What a tool call changes outside its own result.

Before a write runs, its file's previous content is kept (``pop_previous_content``);
once it completes, the file change is recorded with its undo state (:mod:`file_changes`), and a
plugin record row (``_meta["ai.iowarp.clio/ui_event"]``) changes the study's
``records/<view>`` source, so the study's sessions are told (``science.record``) and a
view reading that source re-reads it, with no polling (taskable run_code plan, Part 3).
A published dashboard is told the same way (``science.view``): it may be a study's view.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.file_changes import (
    PreviousContent,
    pop_previous_content,
    record_file_change,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The bus event a study's sessions get when a record row of one of its views lands.
SCIENCE_RECORD_EVENT = "science.record"
#: The bus event a study's sessions get when one of its sessions publishes a dashboard
#: (a study view is a dashboard; its owner revises it by publishing again).
SCIENCE_VIEW_EVENT = "science.view"


def notify_study(app: "FastAPI", sid: str, event_type: str, payload: dict[str, Any]) -> str:
    """Publish ``event_type`` to every session of ``sid``'s study and its tree root, so a
    tab open on any of them re-reads what changed; the study, or ``""`` when none."""

    bus = getattr(app.state, "bus", None)
    if bus is None or not sid:
        return ""
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    log = science_log(app)
    study = log.study_of(sid)
    if not study:
        return ""
    root = log.history.index.session_root(sid)
    body = {"study": study, **payload, "session_id": sid}
    for member in dict.fromkeys((sid, root, *log.study_sessions(study))):
        bus.publish(Event(type=event_type, session_id=member, payload=body))
    return study


def notify_record_change(app: "FastAPI", sid: str, result: Any) -> str | None:
    """Tell the study of ``sid`` that a record view changed; the view's name, or ``None``."""

    event = result.get("uiEvent") if isinstance(result, Mapping) else None
    view = str(event.get("view") or "") if isinstance(event, Mapping) else ""
    if not view or not notify_study(app, sid, SCIENCE_RECORD_EVENT, {"view": view}):
        return None
    return view


def record_call_effects(
    app: "FastAPI",
    sid: str,
    name: str,
    call_id: str,
    error: str | None,
    result: Any,
    previous: PreviousContent | None,
    agent_id: str = "",
) -> None:
    """Record a completed call's file change and announce a record row it carried."""

    record_file_change(app, sid, name, call_id, error, result, previous, agent_id)
    if error is None:
        notify_record_change(app, sid, result)


__all__ = [
    "SCIENCE_RECORD_EVENT",
    "SCIENCE_VIEW_EVENT",
    "notify_record_change",
    "notify_study",
    "pop_previous_content",
    "record_call_effects",
]
