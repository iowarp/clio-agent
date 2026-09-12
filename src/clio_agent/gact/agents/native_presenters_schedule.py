"""Native presenters for the schedule/loop/goal family (#1333 ratchet payment).

Split out of ``native_presenters.py``: ``goal``, ``loop``, ``todos``, ``schedules``,
``schedule_created``, and ``schedule_deleted`` all render recurring/scheduled-work
state and share the human-readable time formatters below.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


def _readable_timestamp(value: Any) -> str:
    """Format an ISO timestamp for people while retaining the precise instant."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    zone = parsed.tzname() or ""
    rendered = (
        f"{parsed.strftime('%b')} {parsed.day}, "
        f"{parsed.strftime('%I').lstrip('0')}:{parsed.strftime('%M %p')}"
    )
    return f"{rendered} {zone}".strip()


def _readable_delay(seconds: Any) -> str:
    """Render a relative trigger interval without hiding its exact scheduled instant."""

    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value % 3600 == 0:
        count, unit = value // 3600, "hour"
    elif value % 60 == 0:
        count, unit = value // 60, "minute"
    else:
        count, unit = value, "second"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def present_schedule_family(
    declaration: str, args: Mapping[str, Any], result: Any, row: Mapping[str, Any], summary: str
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    """Render ``goal``/``loop``/``todos``/``schedules``/``schedule_created``/``schedule_deleted``.

    Returns ``(summary, blocks, header_action, presentation_status, subject)``.
    """

    blocks: list[dict[str, Any]] = []
    header_action = ""
    presentation_status = ""
    subject = ""

    if declaration == "goal":
        header_action = "Get goal status"
        summary = "" if row.get("active") else "There is no goal."
        if row.get("active"):
            blocks.append(
                {
                    "id": "goal",
                    "type": "link",
                    "target": "work",
                    "uri": "session-work",
                    "label": f"Goal active at iteration {row.get('iters_elapsed', 0)}",
                }
            )
        if row.get("condition"):
            blocks.append({"id": "condition", "type": "text", "text": str(row["condition"])})
    elif declaration == "loop":
        if row.get("stopped") is True:
            header_action = "Stop loop"
            summary = "Loop stopped" if row.get("loop_id") else "No active loop"
            reason = str(args.get("reason") or "").strip()
            if reason:
                blocks.append({"id": "reason", "type": "text", "text": reason})
        elif row.get("next_fire_at"):
            header_action = "Schedule next iteration"
            delay = _readable_delay(args.get("delay_seconds"))
            summary = f"Next iteration runs in {delay}" if delay else "Next iteration scheduled"
            details = [
                str(args.get("prompt") or ""),
                f"Runs {_readable_timestamp(row['next_fire_at'])}",
            ]
            blocks.append({"id": "next", "type": "text", "text": "\n".join(filter(None, details))})
    elif declaration == "todos":
        header_action = "Update tasks"
        changes = [change for change in row.get("changes", []) if isinstance(change, Mapping)]
        changed = [change for change in changes if change.get("change") != "unchanged"]
        if changes:
            summary = f"{len(changed)} task{'' if len(changed) == 1 else 's'} changed"
            source = changes
        else:
            source = [todo for todo in row.get("todos", []) if isinstance(todo, Mapping)]
            summary = "" if source else "No tasks in this list"
        for index, todo in enumerate(source):
            block = {
                "id": f"todo-{index}",
                "type": "check",
                "state": todo.get("status", "pending"),
                "text": str(todo.get("content", "")),
            }
            if todo.get("previous_status"):
                block["previous_state"] = todo["previous_status"]
            if todo.get("change"):
                block["change"] = todo["change"]
            blocks.append(block)
    elif declaration == "schedules":
        schedules = [
            schedule for schedule in row.get("schedules", []) if isinstance(schedule, Mapping)
        ]
        if schedules:
            recurring = sum(bool(schedule.get("recurring")) for schedule in schedules)
            one_shot = len(schedules) - recurring
            summary = (
                f"{len(schedules)} schedule{'' if len(schedules) == 1 else 's'}, "
                f"{recurring} recurring, {one_shot} one-shot"
            )
            for index, schedule in enumerate(schedules):
                schedule_id = str(schedule.get("id") or "").strip()
                prompt = str(schedule.get("prompt") or "Scheduled turn").strip()
                details = [
                    "Recurring" if schedule.get("recurring") else "One-shot",
                    f"Runs {_readable_timestamp(schedule.get('next_fire_at'))}",
                    f"Time zone {schedule['timezone']}" if schedule.get("timezone") else "",
                ]
                blocks.append(
                    {
                        "id": f"schedule-{index}",
                        "type": "item",
                        "target": "work",
                        "uri": schedule_id,
                        "label": prompt,
                        "items": [detail for detail in details if detail],
                    }
                )
        else:
            summary = "There are no schedules."
    elif declaration == "schedule_created":
        if row.get("schedule_id"):
            delay = _readable_delay(args.get("delay_s"))
            summary = f"Scheduled to run in {delay}" if delay else ""
            details = []
            if row.get("cron"):
                details.append(str(row["cron"]))
            else:
                details.append("One-shot")
            if row.get("next_fire_at"):
                details.append(f"Runs {_readable_timestamp(row['next_fire_at'])}")
            if row.get("timezone"):
                details.append(f"Time zone {row['timezone']}")
            blocks.append(
                {
                    "id": "schedule",
                    "type": "item",
                    "target": "work",
                    "uri": str(row["schedule_id"]),
                    "label": str(args.get("prompt") or "Scheduled turn"),
                    "items": list(filter(None, details)),
                }
            )
    elif declaration == "schedule_deleted":
        schedule_id = str(row.get("schedule_id") or args.get("schedule_id") or "").strip()
        if schedule_id:
            subject = "schedule-subject"
            blocks.append(
                {
                    "id": subject,
                    "type": "link",
                    "target": "work",
                    "uri": schedule_id,
                    "label": schedule_id,
                }
            )
        deleted = row.get("deleted")
        if deleted is None and isinstance(result, bool):
            deleted = result
        summary = "Schedule deleted." if deleted else "No matching schedule was found."
        if deleted is False:
            presentation_status = "error"

    return summary, blocks, header_action, presentation_status, subject
