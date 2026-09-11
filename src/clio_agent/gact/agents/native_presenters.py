"""Declared native-tool presenters, isolated from execution and observation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

Presenter = Callable[[Mapping[str, Any], Any, Any], dict[str, Any]]


def build_wait_tool(callback: Callable[..., Any]) -> Any:
    """Declare the committed collector's model interface and observer result view."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    return native_tool(
        callback,
        name="wait_agent_tasks",
        presentation="wait",
        presentation_start=waiting_presentation,
        desc=callback.__doc__,
        title="Wait",
        args={"task_ids": {"type": "array", "description": "Task ids returned by spawn."}},
    )


def waiting_presentation(args: Mapping[str, Any]) -> dict[str, Any]:
    """Identify the requested children while the single committed wait is open."""
    from clio_agent.gact import context
    from clio_agent.gact.agent_tasks import resolve_waited_task_rows

    task_ids = args.get("task_ids", [])
    if not isinstance(task_ids, list) or not all(isinstance(tid, str) for tid in task_ids):
        return {"summary": "Waiting for tasks", "blocks": []}
    app = context.active_app()
    rows = resolve_waited_task_rows(app, task_ids) if app is not None else []
    names = [str(row.get("name") or row.get("task_id") or "Task") for row in rows]
    return {
        "summary": "Waiting for " + ", ".join(names or task_ids)
        if task_ids
        else "No tasks requested",
        "blocks": [],
    }


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


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


def _visible_skill_body(result: str, skill_id: str) -> str:
    """Remove the model-facing skill identity already owned by the tool header."""

    lines = result.splitlines()
    if lines and lines[0].strip() == f"# Skill: {skill_id}":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines)
    return result


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


def native_presentation(
    declaration: str, args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Render an explicitly selected native result contract, never guess a tool."""

    row = _record(structured) if structured is not None else _record(result)
    summary = str(row.get("message") or row.get("error") or "")
    blocks: list[dict[str, Any]] = []
    header_action = ""
    presentation_status = ""
    subject = ""
    if declaration == "text":
        if isinstance(result, str):
            skill_id = str(args.get("skill_id") or "").strip()
            visible = _visible_skill_body(result, skill_id) if skill_id else result
            blocks.append({"id": "content", "type": "markdown", "text": visible})
    elif declaration == "task_output":
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
    elif declaration in {"tasks", "wait"}:
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
            if declaration == "tasks":
                for event in task.get("new_events", []):
                    if not isinstance(event, Mapping):
                        continue
                    for key in ("summary", "excerpt"):
                        text = str(event.get(key) or "").strip()
                        if text and text not in details:
                            details.append(text)
            else:
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
                    "result_kind": "snapshot" if declaration == "tasks" else "completion",
                    "duration_ms": (
                        float(display.get("waited_ms") or 0)
                        if declaration == "wait"
                        and isinstance(display.get("waited_ms"), int | float)
                        else None
                    ),
                    "detail": "\n".join(details),
                }
            )
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
    elif declaration == "goal":
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
    elif declaration == "model_catalog":
        entries = 0
        for provider in row.get("results", []):
            if not isinstance(provider, Mapping):
                continue
            entries += 1
            provider_id = str(provider.get("provider") or f"provider-{entries}")
            provider_source = str(provider.get("source") or "")
            default_model = str(provider.get("default_model") or "")
            failed_reason = str(provider.get("failed_reason") or "")
            models: list[str] = []
            for field in ("added", "unchanged"):
                value = provider.get(field)
                if isinstance(value, list):
                    models.extend(str(item) for item in value if isinstance(item, str))
            details = []
            if provider_source:
                details.append(f"Source: {provider_source}")
            if default_model:
                details.append(f"Default model: {default_model}")
            if failed_reason:
                details.append(failed_reason)
            removed = provider.get("removed")
            if isinstance(removed, list) and removed:
                details.append(f"Removed: {', '.join(str(item) for item in removed)}")
            rejected = provider.get("rejected")
            if isinstance(rejected, list) and rejected:
                details.append(f"Rejected: {', '.join(str(item) for item in rejected)}")
            blocks.append(
                {
                    "id": f"provider-{entries}",
                    "type": "item",
                    "target": "url",
                    "uri": f"/settings/providers?provider={provider_id}",
                    "label": provider_id,
                    "status": "failed" if failed_reason else "succeeded",
                    "detail": "\n".join(details),
                    "items": list(dict.fromkeys(models)),
                    "action_label": "Change",
                }
            )
        summary = f"{entries} provider results" if entries else "No provider results"
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
    elif declaration == "memory":
        memory_tool = str(row.get("tool") or "")
        if memory_tool == "memory_search_sessions":
            query = str(row.get("query") or args.get("query") or "").strip()
            if query:
                subject = "query"
                blocks.append({"id": subject, "type": "text", "text": query})
            hits = [hit for hit in row.get("hits", []) if isinstance(hit, Mapping)]
            searched = [str(value) for value in row.get("searched_sessions", []) if str(value)]
            if hits:
                summary = (
                    f"{len(hits)} {'match' if len(hits) == 1 else 'matches'} "
                    f"in {len(searched)} {'session' if len(searched) == 1 else 'sessions'}"
                )
            else:
                summary = "No matching sessions"
            for index, hit in enumerate(hits):
                session_id = str(hit.get("session_id") or "")
                session_title = str(hit.get("session_title") or session_id or "Session")
                if session_id:
                    blocks.append(
                        {
                            "id": f"hit-{index}-session",
                            "type": "link",
                            "target": "session",
                            "uri": session_id,
                            "label": session_title,
                        }
                    )
                excerpt = str(hit.get("text") or "").strip()
                if excerpt:
                    role = str(hit.get("role") or "").strip().replace("_", " ").capitalize()
                    blocks.append(
                        {
                            "id": f"hit-{index}-excerpt",
                            "type": "text",
                            "label": role,
                            "text": excerpt,
                        }
                    )
        elif memory_tool == "memory_read_session_summary":
            remembered = row.get("summary")
            if isinstance(remembered, Mapping):
                session_id = str(remembered.get("session_id") or "")
                session_title = str(remembered.get("title") or session_id or "Session")
                if session_id:
                    subject = "session"
                    blocks.append(
                        {
                            "id": subject,
                            "type": "link",
                            "target": "session",
                            "uri": session_id,
                            "label": session_title,
                        }
                    )
                message_count = remembered.get("message_count")
                state = str(remembered.get("status") or "").strip().replace("_", " ")
                facts = []
                if isinstance(message_count, int):
                    facts.append(
                        f"{message_count} {'message' if message_count == 1 else 'messages'}"
                    )
                if state:
                    facts.append(f"Status: {state.capitalize()}")
                summary = "\n".join(facts) or "Session summary"
                for index, excerpt in enumerate(remembered.get("recent_excerpts", [])):
                    if not isinstance(excerpt, Mapping):
                        continue
                    text = str(excerpt.get("excerpt") or "").strip()
                    if not text:
                        continue
                    role = str(excerpt.get("role") or "").strip().replace("_", " ").capitalize()
                    blocks.append(
                        {
                            "id": f"excerpt-{index}",
                            "type": "text",
                            "label": role,
                            "text": text,
                        }
                    )
        elif memory_tool == "memory_read_context_frame":
            frame = row.get("frame")
            if isinstance(frame, Mapping):
                session_id = str(frame.get("session_id") or "")
                session_link = _session_link(session_id)
                if session_link is not None:
                    subject = "session"
                    session_link["id"] = subject
                    blocks.append(session_link)
                items = [item for item in frame.get("items", []) if isinstance(item, Mapping)]
                summary = (
                    f"{len(items)} retained {'item' if len(items) == 1 else 'items'}"
                    if items
                    else "No retained context items"
                )
                for index, item in enumerate(items):
                    kind = str(item.get("kind") or "Context item").replace("_", " ").capitalize()
                    role = str(item.get("role") or "").strip().replace("_", " ").capitalize()
                    if kind == "Message" and role:
                        kind = f"{role} message"
                    source = str(item.get("display_path") or item.get("path") or "").strip()
                    included = "Included" if item.get("included", True) else "Excluded"
                    reason = str(item.get("reason") or "").strip().replace("_", " ")
                    state = f"{included} from {reason}" if reason else included
                    detail = "\n".join(value for value in (source, state) if value)
                    blocks.append(
                        {
                            "id": f"item-{index}",
                            "type": "text",
                            "label": kind,
                            "text": detail,
                        }
                    )
    elif declaration == "resource":
        summary = str(row.get("name") or row.get("resource_id") or summary)
        if row.get("resources") == []:
            summary = "No workspace resources"
        record = row.get("resource")
        if isinstance(record, Mapping):
            summary = str(
                record.get("display_name")
                or record.get("name")
                or record.get("resource_id")
                or summary
            )
            fields = [str(record["detected_mime"])] if record.get("detected_mime") else []
            size = record.get("received_size", record.get("declared_size"))
            if isinstance(size, int):
                fields.append(f"{size:,} bytes")
            if record.get("revision") is not None:
                fields.append(f"Revision {record['revision']}")
            if record.get("state"):
                fields.append(str(record["state"]).capitalize())
            if fields:
                blocks.append({"id": "identity", "type": "text", "text": "\n".join(fields)})
            if record.get("failure"):
                blocks.append({"id": "failure", "type": "text", "text": str(record["failure"])})
        resource_id = str(
            row.get("resource_id")
            or (record.get("id") if isinstance(record, Mapping) else "")
            or ""
        )
        resource_link = _resource_link(resource_id) if resource_id else None
        if resource_link is not None:
            summary = ""
            blocks.insert(0, resource_link)
        processing = row.get("processing")
        if isinstance(processing, Mapping):
            state = str(processing.get("state") or "")
            message = str(processing.get("message") or (f"Conversion {state}" if state else ""))
            if isinstance(processing.get("progress"), int | float) and state not in {
                "complete",
                "completed",
                "failed",
                "cancelled",
            }:
                message += f"\nProgress: {processing['progress']}%"
            if processing.get("error"):
                message += f"\n{processing['error']}"
            identity = next((block for block in blocks if block["id"] == "identity"), None)
            if message and identity is not None:
                identity["text"] += f"\n{message}"
            elif message:
                blocks.append({"id": "processing", "type": "text", "text": message})
        matches = row.get("matches")
        if isinstance(matches, list):
            if args.get("query"):
                summary = f"{len(matches)} {'match' if len(matches) == 1 else 'matches'} for “{args['query']}”"
            blocks.append(
                {
                    "id": "matches",
                    "type": "text",
                    "text": "\n".join(
                        f"{match.get('line', '')}: {match.get('text', '')}"
                        for match in matches
                        if isinstance(match, Mapping)
                    )
                    or "No matching passages",
                }
            )
        collections = row.get("collections")
        if isinstance(collections, Mapping):
            blocks.append(
                {
                    "id": "outline",
                    "type": "text",
                    "text": "\n".join(
                        f"{str(name).capitalize()}: {count}"
                        for name, count in collections.items()
                        if isinstance(count, int)
                    ),
                }
            )
        node = row.get("node")
        if isinstance(node, Mapping):
            # A document node is declared resource structure, not a model response.
            fields = [
                str(node[key])
                for key in ("title", "text", "content", "caption")
                if isinstance(node.get(key), str)
            ]
            blocks.append({"id": "node", "type": "text", "text": "\n".join(fields)})
        content = row.get("text", row.get("content"))
        if isinstance(content, str):
            resource_name = (
                str(resource_link.get("label") or "") if isinstance(resource_link, Mapping) else ""
            )
            language = _resource_code_language(resource_name)
            if row.get("representation") == "markdown":
                blocks.append({"id": "content", "type": "markdown", "text": content})
            elif language:
                blocks.append(
                    {
                        "id": "content",
                        "type": "code",
                        "language": language,
                        "text": content,
                    }
                )
            else:
                blocks.append({"id": "content", "type": "text", "text": content})
        if row.get("truncated") is True:
            blocks.append(
                {"id": "bounded", "type": "text", "text": "Result truncated by the resource tool"}
            )
        for index, resource in enumerate(row.get("resources", [])):
            if isinstance(resource, Mapping):
                blocks.append(
                    {
                        "id": f"resource-{index}",
                        "type": "link",
                        "target": "resource",
                        "uri": str(resource.get("resource_id") or resource.get("id") or ""),
                        "label": str(resource.get("name") or resource.get("id") or "Resource"),
                    }
                )
    elif declaration == "artifact":
        artifact_rows = row.get("artifacts", [row])
        accepted = 0
        rejected = 0
        for index, artifact in enumerate(artifact_rows):
            if isinstance(artifact, Mapping):
                if artifact.get("accepted") is False:
                    rejected += 1
                    blocks.append(
                        {
                            "id": f"rejection-{index}",
                            "type": "text",
                            "label": "Artifact rejected",
                            "severity": "error",
                            "text": _artifact_rejection_message(artifact),
                        }
                    )
                    continue
                uri = str(artifact.get("uri") or artifact.get("artifact_id") or "")
                if uri:
                    accepted += 1
                    blocks.append(
                        {
                            "id": f"artifact-{index}",
                            "type": "link",
                            "target": "artifact",
                            "uri": uri,
                            "label": str(
                                artifact.get("name") or artifact.get("title") or "Artifact"
                            ),
                        }
                    )
        if rejected:
            presentation_status = "degraded" if accepted else "failed"
            summary = ""
    elif declaration.startswith("fields:"):
        for field in declaration.removeprefix("fields:").split(","):
            value = row.get(field)
            if isinstance(value, str | int | float | bool) and value != "":
                blocks.append(
                    {
                        "id": field,
                        "type": "text",
                        "text": f"{field.replace('_', ' ').capitalize()}: {value}",
                    }
                )
    elif declaration != "specialized":
        raise ValueError(f"Unknown native presentation declaration: {declaration}")
    # Header subjects are explicitly chosen by each result family. The client
    # never guesses arguments or promotes arbitrary output into an action label.
    if declaration in {"tasks", "wait"}:
        item_blocks = [block for block in blocks if block["type"] == "item"]
        if item_blocks:
            if declaration == "wait":
                item_blocks.sort(key=lambda block: float(block.get("duration_ms") or 0))
                blocks = item_blocks
            labels = [str(block.get("label") or "Task") for block in item_blocks]
            subject_text = ", ".join(labels) if len(labels) <= 2 else f"{len(labels)} tasks"
            subject = "task-subject"
            blocks.insert(0, {"id": subject, "type": "text", "text": subject_text})
            summary = ""
    if declaration in {"resource", "task_output", "message", "artifact"}:
        links = [block for block in blocks if block["type"] == "link"]
        if len(links) == 1:
            subject = links[0]["id"]
            if declaration == "task_output":
                task_id = str(row.get("task_id") or args.get("task_id") or "")
                links[0]["label"] = _child_name(task_id) or "Child task"
                summary = ""
            elif declaration == "artifact":
                artifacts = row.get("artifacts", [])
                if len(artifacts) == 1 and artifacts[0].get("accepted") is True:
                    artifact = artifacts[0]
                    header_action = "Create Artifact"
                    outcome = "Created" if artifact.get("created") else "Already registered as"
                    summary = outcome
                    if artifact.get("version"):
                        summary = f"{outcome} version {artifact['version']}"
    if declaration == "text" and args.get("skill_id"):
        subject = "skill-name"
        blocks.insert(0, {"id": subject, "type": "text", "text": str(args["skill_id"])})
        summary = ""
    return {
        **(
            {"action": "Message"}
            if declaration == "message"
            else ({"action": header_action} if header_action else {})
        ),
        **({"subject": subject} if subject else {}),
        **({"status": presentation_status} if presentation_status else {}),
        "summary": summary,
        "blocks": blocks,
    }


def validate_declaration(value: str | Presenter) -> None:
    """Reject missing or unknown declarations when a native tool is registered."""

    if callable(value):
        return
    if value in {
        "text",
        "tasks",
        "wait",
        "task_output",
        "resource",
        "memory",
        "artifact",
        "specialized",
        "todos",
        "schedules",
        "schedule_created",
        "schedule_deleted",
        "model_catalog",
        "loop",
        "message",
        "goal",
    }:
        return
    if isinstance(value, str) and value.startswith("fields:") and value[7:]:
        return
    raise ValueError("Every native tool must declare result presentation semantics")


def _child_session(task_id: str) -> str:
    from clio_agent.gact import context

    app = context.active_app()
    registry = getattr(app.state, "agent_task_registry", None) if app is not None else None
    task = registry.get(task_id) if registry is not None else None
    return str(getattr(task, "child_session_id", "") or "")


def _artifact_rejection_message(artifact: Mapping[str, Any]) -> str:
    """Explain a rejected artifact with user-facing workspace semantics."""

    reason = str(artifact.get("reason") or "rejected")
    raw_name = str(artifact.get("name") or "").strip()
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1] or "This artifact"
    explanations = {
        "escapes_root": f"{name} is outside the active workspace, so it cannot be registered as an artifact.",
        "would_overwrite": (
            f"{name} already exists but is not a registered artifact. "
            "Register the existing file by path or choose another name."
        ),
        "path_missing": f"{name} does not exist, so it cannot be registered as an artifact.",
        "missing": f"{name} does not exist, so it cannot be registered as an artifact.",
        "not_found": f"{name} does not exist, so it cannot be registered as an artifact.",
    }
    return explanations.get(reason, f"{name} was rejected because {reason.replace('_', ' ')}.")


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


def _resource_link(resource_id: str) -> dict[str, Any] | None:
    """Resolve a display name only inside the observing session's workspace."""
    from clio_agent.gact import context

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        return None
    session = app.state.sessions.get(session_id)
    resource_store = getattr(app.state, "resource_store", None)
    if session is None or resource_store is None:
        return None
    record = resource_store.get(session.workspace_id, resource_id)
    if record is None:
        return None
    return {
        "id": "resource",
        "type": "link",
        "target": "resource",
        "uri": resource_id,
        "label": record.name,
    }


def _session_link(session_id: str) -> dict[str, Any] | None:
    """Resolve a retained session to the shared transcript-navigation link."""

    from clio_agent.gact import context

    if not session_id:
        return None
    app = context.active_app()
    if app is None:
        return None
    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    return {
        "id": "session",
        "type": "link",
        "target": "session",
        "uri": session_id,
        "label": session.title or session_id,
    }


def _resource_code_language(name: str) -> str:
    """Return the shared viewer language for a code-bearing resource name."""

    lowered = name.rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1].lower()
    if lowered in {"dockerfile", "makefile"}:
        return {"dockerfile": "dockerfile", "makefile": "make"}[lowered]
    extension = lowered.rsplit(".", maxsplit=1)[-1] if "." in lowered else ""
    return {
        "c": "c",
        "cc": "cpp",
        "cpp": "cpp",
        "css": "css",
        "go": "go",
        "h": "c",
        "hpp": "cpp",
        "html": "html",
        "java": "java",
        "js": "javascript",
        "json": "json",
        "jsx": "jsx",
        "mjs": "javascript",
        "php": "php",
        "ps1": "powershell",
        "py": "python",
        "rb": "ruby",
        "rs": "rust",
        "sh": "shellscript",
        "sql": "sql",
        "toml": "toml",
        "ts": "typescript",
        "tsx": "tsx",
        "vue": "vue",
        "xml": "xml",
        "yaml": "yaml",
        "yml": "yaml",
    }.get(extension, "")
