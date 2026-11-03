"""Execution-phase TODO tool + recitation (P1.5 #1067, campaign #1057).

Owner module for ``write_todos`` — the model-callable checklist tool the agent uses during
the EXECUTION (edit) phase to track a multi-step task. It is deliberately **separate from plan
mode** (Codex's hard rule): plan mode is for authoring the *plan file*, not a checklist, so
``write_todos`` is FORBIDDEN in plan mode (a typed error), exactly as ``plan_exit`` is
forbidden OUTSIDE plan mode (:mod:`clio_agent.gact.plan_mode`). The two tools are mirror
guards on the mode boundary.

**State, not a new store (RULE 4 / no fifth store).** The checklist lives on
``session.metadata['todos']`` — the #948 ``AgentTask`` no-fifth-store projection pattern, NOT
``workflow_state`` (the plugin-declared structured-merge engine) and NOT a new store. Each call
is a **whole-list replacement**: the model sends the complete list, clio replaces it. Per-item
mutation is intentionally not offered — a partial update against a list the model cannot see
atomically is ambiguous.

**Reject ambiguous parallel writes (no silent merge).** clio runs parallel subagents and a
single ReAct step can emit parallel tool calls; two ``write_todos`` calls in the SAME step are
ambiguous (which whole-list wins?), so the second is a typed error rather than a silent
last-writer-wins merge. "Same step" is keyed on the active turn + ReAct step (the loop's step
span, else the step thought), and the check-and-set is serialized under a
module lock so truly-concurrent calls in one step cannot both pass.

**Recitation (Manus).** During execution the current checklist is re-injected compactly into
the model's turn input each turn (:func:`todo_recitation`), reusing the reminder pattern
(``plan_mode.plan_mode_reminder`` / ``enrichment.pending_task_notifications``)
so it survives compaction and fights lost-in-the-middle. It is NEVER recited in plan mode.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from fastapi import FastAPI

#: ``session.metadata`` key holding the whole todo list (no fifth store — the #948 pattern).
_TODOS_METADATA_KEY = "todos"

#: ``session.metadata`` key holding the step key of the last accepted write (same-step guard).
_TODOS_WRITE_STEP_KEY = "todos_write_step"

#: The three canonical todo statuses. ``blocked`` is deliberately NOT one of them: blocked work
#: stays ``pending``/``in_progress`` (never ``completed``) — see the tool guidance.
_TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")

#: Injection source of the recited checklist, and its reminder ``clio.reminder.todos``.
TODOS_SOURCE = "todos"

#: The heading the built-in ``todos`` reminder opens with (stable + greppable; #881 marker
#: discipline). The wording is the template's: a person's edit of it changes the heading.
TODO_RECITATION_MARKER = "## Todo checklist (your current task list — reconcile before finishing)"

#: Compact status glyphs for the recited checklist.
_STATUS_GLYPH: dict[str, str] = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}

#: Serializes the read-check-write of the same-step guard so parallel calls in one step cannot
#: both pass (the session store's own lock covers a single ``update``, not our check-and-set).
_WRITE_LOCK = threading.Lock()


class TodoError(RuntimeError):
    """A ``write_todos`` call was rejected (typed reason — never a silent merge/ignore).

    Carries a machine-readable ``reason`` (``not_in_edit_mode``, ``plan_mode_forbidden``,
    ``parallel_write``, ``invalid_todos``, ``invalid_status``, ``empty_content``,
    ``no_active_session``) so callers/audit can branch without string-matching the message.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _normalize_todos(todos: Any) -> list[dict[str, str]]:
    """Validate + normalize the model's whole todo list to ``[{content, status}, …]`` (typed).

    Raises :class:`TodoError` for a non-list payload, a non-mapping item, an empty ``content``,
    or a ``status`` outside :data:`_TODO_STATUSES` — never coerces garbage into a silent default.
    """

    if isinstance(todos, Mapping) or not isinstance(todos, Sequence) or isinstance(todos, str):
        raise TodoError(
            "write_todos expects 'todos' to be a list of {content, status} objects.",
            reason="invalid_todos",
        )
    out: list[dict[str, str]] = []
    for idx, item in enumerate(todos):
        if not isinstance(item, Mapping):
            raise TodoError(
                f"todo #{idx} must be an object with 'content' and 'status', got {type(item).__name__}.",
                reason="invalid_todos",
            )
        content = str(item.get("content") or "").strip()
        if not content:
            raise TodoError(f"todo #{idx} has empty 'content'.", reason="empty_content")
        status = str(item.get("status") or "").strip().lower()
        if status not in _TODO_STATUSES:
            raise TodoError(
                f"todo #{idx} has invalid status {status!r} (must be one of {list(_TODO_STATUSES)}).",
                reason="invalid_status",
            )
        out.append({"content": content, "status": status})
    return out


def _current_step_key(sid: str) -> str:
    """Return an identity for the CURRENT ReAct step (for the same-step parallel-write guard).

    The loop's step span (unique per step, shared by the step's concurrent tool calls);
    the step thought when no step span is set (a direct call). Includes the turn id so a
    new turn always starts a fresh step namespace.
    """

    turn_id = _ctx.active_turn_id() or ""
    marker = _ctx.active_parent_span_id() or _ctx.active_step_thought() or ""
    return f"{sid}#{turn_id}#{marker}"


def recorded_todos(session: Any) -> list[dict[str, str]]:
    """Return the todo list recorded on ``session.metadata`` (empty when unset)."""

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(_TODOS_METADATA_KEY)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _todo_changes(
    previous: list[dict[str, str]], current: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Describe one whole-list replacement without inventing persistent item IDs.

    Matching is occurrence-aware by content, so duplicate labels remain deterministic. The
    receipt records additions, removals, status transitions, and unchanged items while the
    authoritative current list continues to live only in session metadata.
    """

    unmatched = list(previous)
    changes: list[dict[str, str]] = []
    for todo in current:
        match = next(
            (index for index, old in enumerate(unmatched) if old.get("content") == todo["content"]),
            None,
        )
        if match is None:
            changes.append({**todo, "change": "added"})
            continue
        old = unmatched.pop(match)
        before = str(old.get("status") or "pending")
        change = "unchanged" if before == todo["status"] else "status_changed"
        changes.append({**todo, "previous_status": before, "change": change})
    changes.extend(
        {
            "content": str(todo.get("content") or ""),
            "status": str(todo.get("status") or "pending"),
            "change": "removed",
        }
        for todo in unmatched
    )
    return changes


def _write_todos(app: "FastAPI", sid: str, session: Any, todos: Any) -> str:
    """Validate + apply a whole-list ``write_todos`` (mode-gated, parallel-safe). Returns a
    compact confirmation. Raises :class:`TodoError` (mutating nothing) on any rejection."""

    mode = str(getattr(session, "mode", "") or "edit")
    if mode == "plan":
        raise TodoError(
            "TODO/checklist tool is not allowed in Plan mode — plan mode is for authoring the "
            "plan file, not a task checklist. Write your plan to the plan file instead.",
            reason="plan_mode_forbidden",
        )
    normalized = _normalize_todos(todos)
    step_key = _current_step_key(sid)
    previous: list[dict[str, str]] = []
    with _WRITE_LOCK:
        fresh = app.state.sessions.get(sid)
        metadata = getattr(fresh, "metadata", None) if fresh is not None else None
        previous = recorded_todos(fresh)
        last_step = metadata.get(_TODOS_WRITE_STEP_KEY) if isinstance(metadata, Mapping) else None
        if last_step == step_key:
            raise TodoError(
                "two write_todos calls in the same step are ambiguous (whole-list replacement) — "
                "issue ONE write_todos per step with the complete list.",
                reason="parallel_write",
            )
        app.state.sessions.update(
            sid,
            metadata_patch={_TODOS_METADATA_KEY: normalized, _TODOS_WRITE_STEP_KEY: step_key},
        )
    from clio_agent.gact.work_events import publish_work_updated  # noqa: PLC0415

    publish_work_updated(app, sid, "todos")
    counts = {
        status: sum(1 for t in normalized if t["status"] == status) for status in _TODO_STATUSES
    }
    # P1.6d #1068: the completed-todo count is the TYPED step-advancement signal for an active
    # execution-phase playbook — advance its active step to match (forward-only, no-op without one).
    from clio_agent.gact.planning import advance_execution_step  # noqa: PLC0415

    advance_execution_step(app, sid, completed_todos=counts["completed"])
    confirmation = (
        f"Recorded {len(normalized)} todo(s): "
        f"{counts['completed']} completed, {counts['in_progress']} in_progress, "
        f"{counts['pending']} pending."
    )
    # Declared structured payload (P5 wire semantics — the wait_agent_tasks
    # treatment): the SAME confirmation text as the wire's ``message`` (never a
    # second, divergent phrasing), then the full list + counts. Model-facing
    # return (above) is unchanged.
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    declare_structured_content(
        {
            "message": confirmation,
            "todos": normalized,
            "previous_todos": previous,
            "changes": _todo_changes(previous, normalized),
            "counts": counts,
        }
    )
    return confirmation


def build_write_todos_tool(agent_def: Any) -> Any:
    """Build the ``write_todos`` dspy.Tool — the execution-phase checklist (whole-list replace).

    Auto-attached to every react expert (like ``create_artifact`` / ``plan_exit``). It self-guards
    on mode: a call in PLAN mode hard-errors (plan mode uses the plan file, not a checklist), and
    two calls in one step hard-error (ambiguous whole-list write). State lives on
    ``session.metadata['todos']`` (no fifth store).
    """

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def write_todos(todos: list) -> str:
        """Keep a short checklist of the next concrete steps of a multi-step job.

        Each call replaces the whole list; each todo is
        ``{"content": <str>, "status": "pending"|"in_progress"|"completed"}``. It helps on
        work with three or more steps; a simple task does not need it. Mark an item
        in_progress while you work it and completed when it is actually done: blocked or
        partly done work is not completed, so keep the blocker in its text, and keep the
        requested outcome when you revise an item. Because a call replaces the list, a
        second call in the same step would be ambiguous and is rejected.

        Not available in Plan mode (plan mode authors the plan file instead)."""

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            raise TodoError(
                "write_todos requires an active CLIO app/session context.",
                reason="no_active_session",
            )
        session = app.state.sessions.get(sid)
        if session is None:
            raise TodoError(
                "write_todos could not resolve the active session.", reason="no_active_session"
            )
        return _write_todos(app, sid, session, todos)

    return native_tool(
        write_todos,
        name="write_todos",
        callers="direct",
        presentation="todos",
        domain="planning",
        desc=write_todos.__doc__,
        title="Update Todos",
        args={
            "todos": {
                "type": "array",
                "description": (
                    "The COMPLETE checklist (whole-list replacement). Each item is "
                    '{"content": str, "status": "pending"|"in_progress"|"completed"}.'
                ),
            }
        },
    )


def _render_checklist(todos: list[dict[str, str]]) -> str:
    """Render the compact recited checklist (one glyph-prefixed line per todo)."""

    return "\n".join(
        f"{_STATUS_GLYPH.get(t.get('status', ''), '[ ]')} {t.get('content', '')}" for t in todos
    )


def todo_recitation(app: "FastAPI", sid: str, session: Any) -> str:
    """The current todo checklist as this turn's CLIO addition during EXECUTION (recitation).

    Empty in PLAN mode (the checklist is never recited while planning) and when no todos
    are recorded. Otherwise a compact, marked block (the reminder ``todos``); the agent loop
    records it as its own message, again only when the list changed (never the system prompt).
    """
    from clio_agent.gact import reminders  # noqa: PLC0415

    if str(getattr(session, "mode", "") or "") == "plan":
        return ""
    todos = recorded_todos(session)
    if not todos:
        return ""
    checklist = {"checklist": _render_checklist(todos)}
    return reminders.render(TODOS_SOURCE, checklist, app=app, session_id=sid) or ""
