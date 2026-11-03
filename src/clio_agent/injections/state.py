"""The session state a template may read: ``plan``, ``todos``, ``session``, ``context``.

Only the names a template uses are computed (:func:`state_variables`), from the session
record and the live context counters; nothing is computed for a template that reads
only its event's own variables. Without an app or session the state is not available,
and a template that reads it fails typed (``StrictUndefined``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["state_variables"]


def _plan(session: Any) -> dict[str, Any]:
    from clio_agent.gact.session_plan import plan_state  # noqa: PLC0415

    state = plan_state(session)
    return {"location": state["path"], "status": state["status"], "exists": state["exists"]}


def _todos(session: Any) -> dict[str, Any]:
    from clio_agent.gact.todos import recorded_todos  # noqa: PLC0415

    items = recorded_todos(session)
    done = sum(1 for item in items if str(item.get("status")) == "completed")
    return {"open": len(items) - done, "done": done, "total": len(items), "items": items}


def _session(session: Any, session_id: str) -> dict[str, Any]:
    metadata = getattr(session, "metadata", None) or {}
    return {
        "id": session_id,
        "workspace": str(getattr(session, "workspace_root", "") or ""),
        "delegated": bool(metadata.get("parent_session_id") or metadata.get("parent_id")),
    }


def _context() -> dict[str, Any]:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.runtime.context_tokens import _last_prompt_tokens  # noqa: PLC0415

    window, tokens = _ctx.active_react_context_window(), _last_prompt_tokens()
    percent = round(100 * tokens / window, 1) if window and tokens else 0.0
    return {"tokens": tokens, "window": window, "percent": percent}


def state_variables(names: Iterable[str], app: Any, session_id: str) -> dict[str, Any]:
    """The state values for ``names`` (only the state names among them)."""
    wanted = {"plan", "todos", "session", "context"} & set(names)
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    out: dict[str, Any] = {}
    if session is not None:
        if "plan" in wanted:
            out["plan"] = _plan(session)
        if "todos" in wanted:
            out["todos"] = _todos(session)
        if "session" in wanted:
            out["session"] = _session(session, session_id)
    if "context" in wanted:
        out["context"] = _context()
    return out
