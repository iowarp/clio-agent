"""The session plan: a CLIO-held Markdown document the agent keeps current.

The plan is the file plan mode writes under CLIO's state folder, its path on
``session.metadata['plan_file']`` (no new store) and its status beside it on
``session.metadata['plan_status']``:

- ``draft`` while it is written in plan mode (``plan_enter``);
- ``approved`` after the person approves it at ``plan_exit``;
- ``active`` when the agent wrote it outside plan mode, to carry out itself (no one
  approved it, so it is not called approved);
- ``done`` or ``abandoned`` when the agent says so with ``plan_update``.

``plan_update`` writes the whole plan or replaces one exact passage, in plan mode and
outside it, so the agent replans at any step without a mode switch. While a plan is
``draft``, ``approved`` or ``active`` it is offered as working state (:func:`plan_injection`) at
turn start and after a compaction; todos stay separate (``write_todos``). A plan the agent
wrote itself is already in its context as its ``plan_update`` calls, so it is offered again
only once those calls are gone (a compaction, a rebuilt context) or the text changed under
it (the person's edit).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

__all__ = [
    "ACTIVE_STATUSES",
    "PLAN_SOURCE",
    "PERSON_EDIT_KEY",
    "PLAN_STATUS_KEY",
    "PlanError",
    "build_plan_enter_tool",
    "build_plan_update_tool",
    "mark_person_edit",
    "take_person_edit",
    "plan_injection",
    "plan_state",
    "rail_plan",
    "set_plan_status",
]

PLAN_STATUS_KEY = "plan_status"
#: Digest of the plan text as the agent's last ``plan_update`` call left it (a person's
#: edit through the Work rail does not set it: the agent has not seen that text).
WRITTEN_KEY = "plan_written_digest"
#: The person edited the plan on the Work rail; it is offered to the agent at its next step.
PERSON_EDIT_KEY = "plan_person_edit"
#: Injection source of the plan offered as working state, and its reminder.
PLAN_SOURCE = "plan"
#: Reminders for the person's edit of the plan and for a plan cut to the window's share.
PLAN_PERSON_EDIT_SOURCE = "plan_person_edit"
PLAN_TRUNCATED_SOURCE = "plan_truncated"
#: Statuses in which the plan is offered as working state.
ACTIVE_STATUSES = frozenset({"draft", "approved", "active"})
_SETTABLE = frozenset({"done", "abandoned"})


class PlanError(ValueError):
    """A plan call was refused; ``reason`` is typed."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


#: Typed reason: the window is unknown, so the plan is offered whole.
PLAN_WINDOW_UNKNOWN = "plan_window_unknown"


def _cut_plan(text: str) -> str | None:
    """The part of the plan offered, or ``None`` when it is offered whole.

    The plan is the agent's own file, so the only reason to cut it is the context window:
    like any one result it may use the window's share (``tools.result_share``,
    :mod:`clio_agent.tools.result_room`). The room left is not used: it is measured from
    the last prompt, which is stale at the points the plan is offered (turn start, after a
    compaction). Unknown window: there is nothing to cut to, so it is offered whole, traced
    as :data:`PLAN_WINDOW_UNKNOWN`.
    """
    from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens  # noqa: PLC0415
    from clio_agent.runtime import trace  # noqa: PLC0415
    from clio_agent.tools.result_room import CHARS_PER_TOKEN, window_share_tokens  # noqa: PLC0415

    tokens = window_share_tokens()
    if tokens is None:
        trace.event("TOOLS", "%s: the plan is offered whole", PLAN_WINDOW_UNKNOWN)
        return None
    if _estimate_text_tokens(text) <= tokens:
        return None
    return text[: tokens * CHARS_PER_TOKEN].rstrip()


def plan_state(session: Any) -> dict[str, Any]:
    """``{path, status, text, exists}`` for a session's plan (text ``""`` when absent)."""
    metadata = getattr(session, "metadata", None) or {}
    path = str(metadata.get("plan_file") or "")
    status = str(metadata.get(PLAN_STATUS_KEY) or "")
    text = ""
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            text = ""
    return {"path": path, "status": status, "text": text, "exists": bool(path and text)}


def rail_plan(session: Any) -> dict[str, Any] | None:
    """The plan as the Work rail shows it, or ``None`` when the session has none."""
    state = plan_state(session)
    if not state["exists"]:
        return None
    return {"path": state["path"], "status": state["status"] or "draft", "text": state["text"]}


def set_plan_status(app: Any, session_id: str, status: str) -> None:
    """Record the plan's status (``approved`` is set when plan_exit is approved)."""
    app.state.sessions.update(session_id, metadata_patch={PLAN_STATUS_KEY: status})


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def plan_injection(app: Any, session_id: str, session: Any, *, force_full: bool = False) -> str:
    """The plan as working state, while it is active (:data:`ACTIVE_STATUSES`) outside plan mode.

    In plan mode the plan-mode reminder already names the file. The model judges whether
    the plan still applies; CLIO only says it exists and shows it. A plan the person edited
    on the Work rail is offered in any mode, once, marked as theirs. The wording is the
    reminders ``plan``, ``plan_person_edit`` and ``plan_truncated``.

    A plan whose text is what the agent's last ``plan_update`` wrote is returned
    :func:`~clio_agent.injections.held_by` ``plan_update``: the recorder skips it while
    such a call is in context. ``force_full`` (right after a compaction) offers it plainly.
    """
    from clio_agent import injections  # noqa: PLC0415

    state = plan_state(session)
    if not state["exists"]:
        return ""
    where = {"app": app, "session_id": session_id}
    text = state["text"].strip()
    if (cut := _cut_plan(text)) is not None:
        note = injections.render(PLAN_TRUNCATED_SOURCE, {"path": state["path"]}, **where)
        text = cut + (f"\n\n{note}" if note else "")
    if (getattr(session, "metadata", None) or {}).get(PERSON_EDIT_KEY):
        edited = injections.render(
            PLAN_PERSON_EDIT_SOURCE, {"path": state["path"], "plan": text}, **where
        )
        if edited is not None:  # switched off: the plan is offered as usual below
            return edited
    if str(getattr(session, "mode", "") or "") == "plan" or state["status"] not in ACTIVE_STATUSES:
        return ""
    variables = {"status": state["status"], "path": state["path"], "plan_text": text}
    offered = injections.render(PLAN_SOURCE, variables, **where) or ""
    written = (getattr(session, "metadata", None) or {}).get(WRITTEN_KEY)
    if offered and not force_full and written == _digest(state["text"]):
        return injections.held_by(offered, "plan_update")
    return offered


def mark_person_edit(app: Any, session_id: str) -> None:
    """The person saved an edit of the plan: offer it to the agent at its next step."""
    app.state.sessions.update(session_id, metadata_patch={PERSON_EDIT_KEY: True})


def take_person_edit(app: Any, session_id: str) -> str:
    """The person's edited plan to offer now (``""`` when there is none), then clear the mark."""
    session = app.state.sessions.get(session_id)
    if session is None or not (session.metadata or {}).get(PERSON_EDIT_KEY):
        return ""
    text = plan_injection(app, session_id, session)
    app.state.sessions.update(session_id, metadata_patch={PERSON_EDIT_KEY: False})
    return text


def _session(app: Any, session_id: str) -> Any:
    session = app.state.sessions.get(session_id)
    if session is None:
        raise PlanError("The conversation could not be found.", "no_active_session")
    return session


def _plan_path(app: Any, session_id: str, session: Any) -> Path:
    from clio_agent.gact.plan_mode import (  # noqa: PLC0415
        _compute_plan_file_path,
        _session_plans_dir,
    )
    from clio_agent.gact.plan_review import ensure_owned_plan_directory  # noqa: PLC0415

    recorded = str((getattr(session, "metadata", None) or {}).get("plan_file") or "")
    path = Path(recorded) if recorded else _compute_plan_file_path(app, session_id, session)
    owned = _session_plans_dir(app, session, fallback=path.resolve(strict=False).parent)
    ensure_owned_plan_directory(str(path), owned_directory=owned)
    if not recorded:
        app.state.sessions.update(session_id, metadata_patch={"plan_file": str(path)})
    return path


def enter_plan(app: Any, session_id: str, reason: str = "") -> str:
    """Switch the session to plan mode; the plan-mode reminder is the result."""
    from clio_agent.gact.plan_mode import plan_mode_reminder  # noqa: PLC0415

    session = _session(app, session_id)
    if str(getattr(session, "mode", "") or "") != "plan":
        app.state.sessions.update(
            session_id, mode="plan", metadata_patch={PLAN_STATUS_KEY: "draft"}
        )
    session = _session(app, session_id)
    _plan_path(app, session_id, session)
    session = _session(app, session_id)
    reminder = plan_mode_reminder(app, session_id, session, force_full=True)
    head = f"Plan mode entered{f' ({reason.strip()})' if reason.strip() else ''}."
    return f"{head}\n\n{reminder}".strip()


def update_plan(
    app: Any,
    session_id: str,
    *,
    content: str | None = None,
    old: str | None = None,
    new: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Write the plan or one exact passage of it, and/or set its status."""
    session = _session(app, session_id)
    if content is None and old is None and not status:
        raise PlanError("Give content, an old/new passage, or a status.", "plan_update_empty")
    if status and status not in _SETTABLE:
        raise PlanError("status may be done or abandoned.", "plan_status_invalid")
    path = _plan_path(app, session_id, session)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    change = ""
    if content is not None:
        if not content.strip():
            raise PlanError("content is empty; give the whole plan.", "plan_content_empty")
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        change = "whole"
    elif old is not None:
        if new is None:
            raise PlanError("Give new with old.", "plan_passage_incomplete")
        count = before.count(old) if old else 0
        if count != 1:
            raise PlanError(
                f"old must appear exactly once in the plan; it appears {count} times.",
                "plan_passage_not_unique" if count else "plan_passage_missing",
            )
        path.write_text(before.replace(old, new, 1), encoding="utf-8")
        change = "passage"
    metadata = getattr(session, "metadata", None) or {}
    current = str(metadata.get(PLAN_STATUS_KEY) or "")
    if status:
        next_status = status
    elif current in _SETTABLE or not current:
        # Writing a plan after it was closed (or before any) makes it current again.
        next_status = "draft" if str(getattr(session, "mode", "")) == "plan" else "active"
    else:
        next_status = current
    if next_status != current:
        set_plan_status(app, session_id, next_status)
    _publish_work(app, session_id)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {
        "plan_file": str(path),
        "status": next_status,
        "change": change or "status",
        "old": old if change == "passage" else None,
        "new": new if change == "passage" else None,
        "chars": len(text),
        # A fact for a plan closed as done: threads its own text still marks open or
        # active (the states core.plan names). On L1P two of seven runs closed a plan
        # this way, its finished work never recorded. The agent decides what to do.
        **(_thread_counts(text) if next_status == "done" else _NO_COUNT),
    }


_NO_COUNT = {"unfinished_threads": 0, "marked_threads": 0}
_STATE = r"(open|active|closed|dropped)"
#: A list item's state marker, in the forms plans are written in: ``- [open] x`` (the
#: form ``core.plan`` and the plan structure name), ``- x: open``, ``- x (open)`` and
#: ``- x (closed: why)``. A colon form needs the state word to end the phrase, so
#: ``- note: open questions remain`` is not a marker.
_MARKER = re.compile(
    rf"(?i)\[{_STATE}\]|\({_STATE}\b[^)\n]*\)|:\s*{_STATE}\s*(?=$|[(\-\u2013\u2014,.;])"
)
_ITEM = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def _thread_counts(text: str) -> dict[str, int]:
    """``unfinished_threads`` (marked open or active) and ``marked_threads`` (marked any
    state) among the plan's list items; the first marker on a line is its state."""
    states = []
    for item in _ITEM.findall(text):
        found = _MARKER.search(item)
        if found:
            states.append(next(group for group in found.groups() if group).lower())
    unfinished = sum(1 for state in states if state in {"open", "active"})
    return {"unfinished_threads": unfinished, "marked_threads": len(states)}


def _publish_work(app: Any, session_id: str) -> None:
    from clio_agent.gact.work_events import publish_work_updated  # noqa: PLC0415

    publish_work_updated(app, session_id, "plan")


def _active(tool: str) -> tuple[Any, str]:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    if app is None or not session_id:
        raise PlanError(f"{tool} needs an active conversation.", "no_active_session")
    return app, session_id


def build_plan_enter_tool() -> Any:
    """``plan_enter``: start planning in plan mode (read-only except the plan)."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def plan_enter(reason: str = "") -> str:
        """Enter plan mode to investigate and write a plan before changing anything."""
        app, session_id = _active("plan_enter")
        return enter_plan(app, session_id, reason)

    return native_tool(
        plan_enter,
        name="plan_enter",
        callers="direct",
        presentation="text",
        domain="planning",
        title="Enter plan mode",
        desc=(
            "Enter plan mode when the person should approve a plan before anything changes: "
            "you investigate with read tools, write the plan with plan_update, and hand it "
            "back for approval with plan_exit. Nothing else is changed in plan mode. A plan "
            "you write and then carry out yourself needs no plan mode: write it with plan_update."
        ),
        args={"reason": {"type": "string", "description": "Why planning first helps here."}},
    )


def build_plan_update_tool() -> Any:
    """``plan_update``: write the plan, replace a passage, or close it."""
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
        native_tool,
    )
    from clio_agent.gact.session_plan_presentation import present  # noqa: PLC0415

    def plan_update(
        content: str | None = None,
        old: str | None = None,
        new: str | None = None,
        status: str | None = None,
    ) -> str:
        """Write the whole plan, replace one exact passage, or mark it done or abandoned."""
        app, session_id = _active("plan_update")
        result = update_plan(app, session_id, content=content, old=old, new=new, status=status)
        if result["change"] != "status":  # the agent holds this text as this call
            text = Path(result["plan_file"]).read_text(encoding="utf-8")
            app.state.sessions.update(session_id, metadata_patch={WRITTEN_KEY: _digest(text)})
        declare_structured_content(result)
        what = {"whole": "Plan written", "passage": "Plan passage replaced"}.get(
            result["change"], "Plan status set"
        )
        reply = f"{what} ({result['status']}, {result['plan_file']})."
        if result["status"] == "done" and not result["marked_threads"]:
            reply += (
                " No thread in the plan carries a state marker such as [open] or [closed],"
                " so CLIO cannot tell whether any is still open."
            )
        elif result["unfinished_threads"]:
            count = result["unfinished_threads"]
            reply += (
                f" The plan still marks {count} thread{'s' if count != 1 else ''} open or active."
            )
        return reply

    return native_tool(
        plan_update,
        name="plan_update",
        callers="direct",
        presentation=present,
        domain="planning",
        title="Update plan",
        desc=(
            "Write the session plan and keep it current, in plan mode or not (outside plan mode "
            "the work goes on): content writes the whole plan; old and new replace one exact "
            "passage; status done or abandoned closes it. "
            "Update it when a finding changes your understanding or direction, not every step."
        ),
        args={
            "content": {"type": "string", "description": "The whole plan, as Markdown."},
            "old": {"type": "string", "description": "A passage that appears once in the plan."},
            "new": {"type": "string", "description": "What replaces old."},
            "status": {"type": "string", "enum": ["done", "abandoned"]},
        },
    )
