"""Execution-phase TODO tool + recitation (P1.5 #1067, campaign #1057).

Owner module for ``write_todos`` — the model-callable checklist tool the agent uses during
the EXECUTION (edit) phase to track a multi-step task. It is deliberately **separate from plan
mode** (Codex's hard rule): plan mode is for authoring the *plan file*, not a checklist, so
``write_todos`` is FORBIDDEN in plan mode (a typed error), exactly as ``plan_exit`` is
forbidden OUTSIDE plan mode (:mod:`clio_agent.gact.plan_mode`). The two tools are mirror
guards on the mode boundary.

**State, not a new store (RULE 4 / no fifth store).** The checklist lives on
``session.metadata['todos']`` — the #948 ``AgentTask`` no-fifth-store projection pattern, NOT
``workflow_state`` (the pack/blueprint structured-merge engine) and NOT a new store. Each call
is a **whole-list replacement**: the model sends the complete list, clio replaces it. Per-item
mutation is intentionally not offered — a partial update against a list the model cannot see
atomically is ambiguous.

**Reject ambiguous parallel writes (no silent merge).** clio runs parallel subagents and a
single ReAct step can emit parallel tool calls; two ``write_todos`` calls in the SAME step are
ambiguous (which whole-list wins?), so the second is a typed error rather than a silent
last-writer-wins merge. "Same step" is keyed on the active turn + ReAct step (the trajectory
step index when available, else the step thought), and the check-and-set is serialized under a
module lock so truly-concurrent calls in one step cannot both pass.

**Recitation (Manus).** During execution the current checklist is re-injected compactly into
the model's turn input each turn (:func:`inject_todo_recitation`), reusing the reminder pattern
(``plan_mode.inject_plan_mode_reminder`` / ``enrichment.inject_pending_agent_task_notifications``)
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

#: Marker heading the recited checklist block (stable + greppable; #881 marker discipline).
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

    Prefers the trajectory step index (monotonic per step, robust to identical thoughts); falls
    back to the step thought when no trajectory is installed (e.g. a direct call). Includes the
    turn id so a new turn always starts a fresh step namespace.
    """

    turn_id = _ctx.active_turn_id() or ""
    traj = _ctx.active_trajectory()
    if isinstance(traj, Mapping):
        step_idx = sum(1 for k in traj if isinstance(k, str) and k.startswith("thought_"))
        marker = f"traj{step_idx}"
    else:
        marker = _ctx.active_step_thought() or ""
    return f"{sid}#{turn_id}#{marker}"


def recorded_todos(session: Any) -> list[dict[str, str]]:
    """Return the todo list recorded on ``session.metadata`` (empty when unset)."""

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(_TODOS_METADATA_KEY)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


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
    with _WRITE_LOCK:
        fresh = app.state.sessions.get(sid)
        metadata = getattr(fresh, "metadata", None) if fresh is not None else None
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
    counts = {
        status: sum(1 for t in normalized if t["status"] == status) for status in _TODO_STATUSES
    }
    # P1.6d #1068: the completed-todo count is the TYPED step-advancement signal for an active
    # execution-phase playbook — advance its active step to match (forward-only, no-op without one).
    from clio_agent.gact.planning import advance_execution_step  # noqa: PLC0415

    advance_execution_step(app, sid, completed_todos=counts["completed"])
    return (
        f"Recorded {len(normalized)} todo(s): "
        f"{counts['completed']} completed, {counts['in_progress']} in_progress, "
        f"{counts['pending']} pending."
    )


def build_write_todos_tool(agent_def: Any) -> Any:
    """Build the ``write_todos`` dspy.Tool — the execution-phase checklist (whole-list replace).

    Auto-attached to every react expert (like ``create_artifact`` / ``plan_exit``). It self-guards
    on mode: a call in PLAN mode hard-errors (plan mode uses the plan file, not a checklist), and
    two calls in one step hard-error (ambiguous whole-list write). State lives on
    ``session.metadata['todos']`` (no fifth store).
    """

    import dspy  # noqa: PLC0415

    def write_todos(todos: list) -> str:
        """Record your task checklist for a multi-step job (execution phase only).

        Pass the COMPLETE list every call — this REPLACES the whole list (there is no per-item
        update). Each todo is ``{"content": <str>, "status": "pending"|"in_progress"|"completed"}``.

        Guidance:
        - Always transition a todo pending -> in_progress -> completed; never jump straight from
          pending to completed (mark it in_progress while you work it).
        - Multiple todos may be in_progress at once (clio runs parallel subagents), but keep it
          honest — only mark in_progress what is actually being worked.
        - blocked work is NOT completed: leave a blocked item pending or in_progress and note the
          blocker in its content; never mark it completed to move on.
        - Reconcile before finishing: no todo should be left pending/in_progress when the task is
          done — the final list should reflect reality.
        - Issue exactly ONE write_todos per step with the full list; two in one step is rejected.

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

    return dspy.Tool(
        func=write_todos,
        name="write_todos",
        desc=write_todos.__doc__,
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


def inject_todo_recitation(app: "FastAPI", sid: str, session: Any, enriched_text: str) -> str:
    """Prepend the current todo checklist to this turn's input during EXECUTION (Manus recitation).

    Returns ``enriched_text`` unchanged in PLAN mode (the checklist is never recited while
    planning) and when no todos are recorded. Otherwise it prepends a compact, marked block so
    the list stays in the model's recent context and survives compaction — reusing the same
    per-turn-input reminder mechanism as the plan-mode reminder (never the system prompt).
    """

    if str(getattr(session, "mode", "") or "") == "plan":
        return enriched_text
    todos = recorded_todos(session)
    if not todos:
        return enriched_text
    return (
        TODO_RECITATION_MARKER + "\n\n" + _render_checklist(todos) + "\n\n---\n\n" + enriched_text
    )
