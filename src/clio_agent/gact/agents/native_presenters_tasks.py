"""Native presenters for the task family: ``task_output``/``tasks``/``wait``/``message``.

Split out of ``native_presenters.py`` (#1333 ratchet payment) — these four declarations
all render observations of spawned child tasks (labels, child-session links, curated
evidence excerpts), so they share the observe-status helpers below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.agents.native_presenters import _record

_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "succeeded", "denied"}
)


def _child_session(task_id: str) -> str:
    from clio_agent.gact import context

    app = context.active_app()
    registry = getattr(app.state, "agent_task_registry", None) if app is not None else None
    task = registry.get(task_id) if registry is not None else None
    return str(getattr(task, "child_session_id", "") or "")


def _child_name(task_id: str) -> str:
    """Resolve the recorded child label without exposing an opaque task id."""
    from clio_agent.gact import context
    from clio_agent.gact.agent_tasks import display_run_name

    app = context.active_app()
    registry = getattr(app.state, "agent_task_registry", None) if app is not None else None
    task = registry.get(task_id) if registry is not None else None
    if task is None:
        return ""
    agent_id = str(task.agent_ref.get("expert_id") or task.agent_ref.get("blueprint_id") or "Task")
    return display_run_name(agent_id, task.run_index, task.run_label)


def _context_leaf_lines(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    """Describe collected structured context as bounded human-readable facts."""

    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, child in value.items():
            label = str(key).replace("_", " ").strip()
            lines.extend(_context_leaf_lines(child, (*path, label)))
        return lines
    if isinstance(value, list):
        lines = []
        for index, child in enumerate(value, start=1):
            lines.extend(_context_leaf_lines(child, (*path, str(index))))
        return lines
    label = " ".join(part for part in path if part).strip()
    rendered = "yes" if value is True else "no" if value is False else str(value)
    return [f"{label.capitalize()}: {rendered}" if label else rendered]


def _received_context_detail(task: Mapping[str, Any], fallback: str) -> str:
    """Show the meaningful child answer and structured state that entered context."""

    answer = str(task.get("output") or fallback or "").strip()
    workflow_state = task.get("workflow_state")
    structured_lines = _context_leaf_lines(workflow_state) if workflow_state else []
    generic_answers = {"complete", "completed", "done", "success", "succeeded"}
    lines = (
        []
        if structured_lines and answer.lower() in generic_answers
        else ([answer] if answer else [])
    )
    lines.extend(line for line in structured_lines if line and line not in lines)
    detail = "\n".join(lines)
    if len(detail) <= 1800:
        return detail
    return f"{detail[:1740].rstrip()}\nMore context is available in technical details"


def _observe_task_status(task: Mapping[str, Any]) -> str:
    """Describe one observed child without presenting observation as collection."""

    error = str(task.get("error") or "").strip()
    status = str(task.get("status") or "").strip().lower()
    if error:
        return f"terminal ({error.replace('_', ' ')})"
    if status in _TERMINAL_TASK_STATUSES:
        return f"terminal ({status})"
    return status or "unknown"


def _observe_status_line(
    args: Mapping[str, Any], row: Mapping[str, Any], tasks: list[tuple[str, Mapping[str, Any]]]
) -> str:
    """Summarize the exact cursor read, hold outcome, and child states."""

    cursor = row.get("cursor", args.get("cursor", 1))
    next_cursor = row.get("next_cursor", cursor)
    parts = [f"Cursor {cursor} -> {next_cursor}"]
    pattern = str(args.get("pattern") or "").strip()
    if pattern:
        if row.get("matched") is True:
            parts.append(f'Pattern "{pattern}" matched')
        elif any(_observe_task_status(task).startswith("terminal") for _, task in tasks):
            parts.append(f'Pattern "{pattern}" did not match; hold released by terminal child')
        else:
            parts.append(f'Pattern "{pattern}" did not match')
    else:
        parts.append("No pattern; returned immediately")
    parts.extend(f"{label}: {_observe_task_status(task)}" for label, task in tasks)
    return "; ".join(parts)


def _observe_evidence(task: Mapping[str, Any]) -> str:
    """Return only curated observe excerpts, excluding child lifecycle chatter."""

    details: list[str] = []
    for event in task.get("new_events", []):
        if not isinstance(event, Mapping):
            continue
        if event.get("family") == "lifecycle" or event.get("event_type") == (
            "expert.lifecycle.started"
        ):
            continue
        text = str(event.get("excerpt") or event.get("summary") or "").strip()
        if text and text not in details:
            details.append(text)
    return "\n".join(details)


def present_task_family(
    declaration: str, args: Mapping[str, Any], result: Any, row: Mapping[str, Any], summary: str
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    """Render ``task_output``/``tasks``/``wait``/``message`` and their header subjects.

    Returns ``(summary, blocks, header_action, presentation_status, subject)``.
    """

    blocks: list[dict[str, Any]] = []
    header_action = ""
    presentation_status = ""
    subject = ""

    if declaration == "task_output":
        task_id = str(row.get("task_id", args.get("task_id", "")))
        semantic_error = str(row.get("error") or row.get("error_reason") or "").strip()
        summary = ""
        output = str(row.get("output") or "")
        if output:
            blocks.append({"id": "output", "type": "markdown", "text": output})
        if semantic_error:
            presentation_status = "failed"
            blocks.append(
                {
                    "id": "error",
                    "type": "text",
                    "label": "Full output unavailable",
                    "severity": "error",
                    "text": semantic_error.replace("_", " "),
                }
            )
        child = _child_session(str(row.get("task_id") or args.get("task_id") or ""))
        if child:
            blocks.insert(
                0,
                {
                    "id": "child",
                    "type": "link",
                    "target": "session",
                    "uri": child,
                    "label": _child_name(task_id) or "Child task",
                },
            )
        links = [block for block in blocks if block["type"] == "link"]
        if len(links) == 1:
            subject = links[0]["id"]
            links[0]["label"] = _child_name(task_id) or "Child task"
            summary = ""
    elif declaration == "tasks":
        result_record = _record(result)
        task_rows = result_record.get(
            "results", result_record.get("tasks", row.get("results", row.get("tasks", [])))
        )
        display_rows = row.get("results", [])
        observed: list[tuple[str, Mapping[str, Any]]] = []
        child_links: list[dict[str, Any]] = []
        evidence_blocks: list[dict[str, Any]] = []
        for index, task in enumerate(task_rows):
            if not isinstance(task, Mapping):
                continue
            task_id = str(task.get("task_id") or task.get("id") or "")
            display = display_rows[index] if index < len(display_rows) else {}
            label = str(
                display.get("name") or task.get("name") or _child_name(task_id) or task_id or "Task"
            )
            child = str(
                task.get("child_session_id") or task.get("session_id") or _child_session(task_id)
            )
            observed.append((label, task))
            if child:
                child_links.append(
                    {
                        "id": f"task-{index}",
                        "type": "link",
                        "target": "session",
                        "uri": child,
                        "label": label,
                    }
                )
            evidence = _observe_evidence(task)
            if evidence:
                evidence_blocks.append(
                    {
                        "id": f"evidence-{index}",
                        "type": "text",
                        "label": f"Evidence · {label}",
                        "text": evidence,
                    }
                )
        if len(observed) == 1 and child_links:
            blocks.append({**child_links[0], "id": "task-subject"})
        else:
            labels = [label for label, _task in observed]
            subject_text = ", ".join(labels) if len(labels) <= 2 else f"{len(labels)} tasks"
            blocks.append(
                {"id": "task-subject", "type": "text", "text": subject_text or "No tasks"}
            )
            blocks.extend(child_links)
        blocks.append(
            {
                "id": "observation",
                "type": "text",
                "label": "Observed",
                "text": _observe_status_line(args, result_record or row, observed),
            }
        )
        blocks.extend(evidence_blocks)
        summary = ""
        subject = "task-subject"
    elif declaration == "wait":
        summary = str(row.get("summary") or summary)
        result_record = _record(result)
        task_rows = result_record.get(
            "results", result_record.get("tasks", row.get("results", row.get("tasks", [])))
        )
        display_rows = row.get("results", [])
        for index, task in enumerate(task_rows):
            if not isinstance(task, Mapping):
                continue
            task_id = str(task.get("task_id") or task.get("id") or "")
            display = display_rows[index] if index < len(display_rows) else {}
            label = str(
                display.get("name") or task.get("name") or _child_name(task_id) or task_id or "Task"
            )
            status = "failed" if task.get("error") else str(task.get("status") or "")
            child = str(
                task.get("child_session_id") or task.get("session_id") or _child_session(task_id)
            )
            details: list[str] = []
            detail = _received_context_detail(
                task,
                str(display.get("answer_excerpt") or "").strip(),
            )
            if detail:
                details.append(detail)
            blocks.append(
                {
                    "id": f"task-{index}",
                    "type": "item",
                    "target": "session" if child else None,
                    "uri": child,
                    "label": label,
                    "status": status,
                    "result_kind": "completion",
                    "duration_ms": (
                        float(display.get("waited_ms") or 0)
                        if isinstance(display.get("waited_ms"), int | float)
                        else None
                    ),
                    "detail": "\n".join(details),
                }
            )
        item_blocks = [block for block in blocks if block["type"] == "item"]
        if item_blocks:
            item_blocks.sort(key=lambda block: float(block.get("duration_ms") or 0))
            blocks = item_blocks
            labels = [str(block.get("label") or "Task") for block in item_blocks]
            subject_text = ", ".join(labels) if len(labels) <= 2 else f"{len(labels)} tasks"
            subject = "task-subject"
            blocks.insert(0, {"id": subject, "type": "text", "text": subject_text})
            summary = ""
    elif declaration == "message":
        task_id = str(row.get("task_id") or args.get("task_id") or "")
        child = _child_session(task_id)
        if child:
            blocks.append(
                {
                    "id": "recipient",
                    "type": "link",
                    "target": "session",
                    "uri": child,
                    "label": _child_name(task_id) or task_id or "Recipient",
                }
            )
        action = row.get("action")
        action_summary = (
            {"queue": "Queued", "wake": "Follow-up started"}.get(action, summary)
            if isinstance(action, str)
            else summary
        )
        summary = "" if not row.get("error") else str(action_summary)
        if row.get("error"):
            presentation_status = "failed"
            reason = str(row.get("error") or "message_rejected")
            detail = str(row.get("detail") or row.get("message") or "").strip()
            readable = {
                "child_not_running": "The child was not active, so the message was not sent.",
                "unknown_task": "The child task was not found, so the message was not sent.",
            }.get(reason, "The child rejected the message.")
            blocks.append(
                {
                    "id": "error",
                    "type": "text",
                    "label": "Message not sent",
                    "severity": "error",
                    "text": f"{readable}{f' {detail}' if detail else ''}",
                }
            )
        sent_message = str(args.get("message") or "").strip()
        if sent_message:
            blocks.append(
                {
                    "id": "message",
                    "type": "item",
                    "result_kind": "message",
                    "label": "Message attempted" if row.get("error") else "Message sent",
                    "text": sent_message,
                }
            )
        links = [block for block in blocks if block["type"] == "link"]
        if len(links) == 1:
            subject = links[0]["id"]

    return summary, blocks, header_action, presentation_status, subject
