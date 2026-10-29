"""``GET /v1/sessions/{sid}/science-log``: the study a session belongs to, for the
Science log tab: its branches (the sessions holding its entries, with how each relates
to the others), every entry with its current status and links, and the study's views
(Addendum B): each attached dashboard with its owner's current surface.

A session that is in no study gets ``{"in_study": false}``, and the tab stays hidden.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.off_loop import run_off_loop


def _branch(app: Any, session_id: str, root: str) -> dict[str, Any]:
    from clio_agent.gact.session_kind import session_kind  # noqa: PLC0415

    history = app.state.task_history
    session = app.state.sessions.get(session_id)
    metadata = dict(getattr(session, "metadata", None) or {})
    task = history.latest_assignment(session_id)
    origin = metadata.get("fork_origin") or {}
    step = metadata.get("workflow_step_of") or {}
    parent_task = (
        history.task_snapshot(task.owner.parent_handle)
        if task is not None and task.owner.parent_handle
        else None
    )
    if task is not None and task.fork_of:
        relation = "fork"
    elif session_id == root:
        relation = "root"
    elif origin and session_kind(session) == "branch":
        relation = "fork" if (origin.get("author") or {}).get("kind") == "agent" else "branch"
    elif step:
        relation = "step"
    else:
        relation = "delegated"
    forked = history.task_snapshot(task.fork_of) if task is not None and task.fork_of else None
    return {
        "session_id": session_id,
        "title": str(metadata.get("subagent_name") or getattr(session, "title", "") or ""),
        "agent": str((getattr(session, "agent", None) or {}).get("id") or ""),
        "relation": relation,
        "parent_session_id": parent_task.owner.session_id
        if parent_task is not None
        else str(origin.get("session_id") or getattr(session, "parent_session_id", "") or ""),
        "fork_of_session_id": forked.owner.session_id if forked is not None else "",
        "workflow_step": str(step.get("step") or ""),
        "node_folder": str(metadata.get("node_folder") or ""),
        "handle": task.handle if task is not None else "",
        "task_status": task.status if task is not None else "",
        "note": str(
            origin.get("note") or (task.description if relation == "fork" and task else "") or ""
        ),
    }


def science_log_view(app: Any, sid: str) -> dict[str, Any]:
    """The Science log tab's data for session ``sid`` (off the loop)."""

    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415
    from clio_agent.gact.science_log.views import study_views  # noqa: PLC0415

    log = science_log(app)
    study = log.study_of(sid)
    if study is None:
        return {"session_id": sid, "in_study": False}
    entries = log.effective(study)
    sessions = log.study_sessions(study)
    root = next(
        (e["branch"] for e in entries if e["id"] == study), sessions[0] if sessions else sid
    )
    branch_ids = list(dict.fromkeys([root, *sessions, sid]))
    return {
        "session_id": sid,
        "in_study": True,
        "study": study,
        "root_session_id": root,
        "branches": [_branch(app, branch, root) for branch in branch_ids],
        "entries": entries,
        "views": study_views(app, entries, root),
    }


def register_science_log_routes(app: FastAPI) -> None:
    """Register the read routes the Science log tab and its views use."""

    from clio_agent.gact.routes.study_sources import register_study_source_routes  # noqa: PLC0415

    register_study_source_routes(app)

    @app.get("/v1/sessions/{sid}/science-log")
    async def get_science_log(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(status_code=404, detail=f"session not found: {sid}")
        return await run_off_loop(science_log_view, app, sid)


__all__ = ["register_science_log_routes", "science_log_view"]
