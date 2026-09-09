"""Declared native-tool presenters, isolated from execution and observation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
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


def native_presentation(
    declaration: str, args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Render an explicitly selected native result contract, never guess a tool."""

    row = _record(structured) if structured is not None else _record(result)
    summary = str(row.get("message") or row.get("error") or "")
    blocks: list[dict[str, Any]] = []
    header_action = ""
    presentation_status = ""
    if declaration == "text":
        if isinstance(result, str):
            blocks.append({"id": "content", "type": "markdown", "text": result})
    elif declaration == "task_output":
        task_id = str(row.get("task_id", args.get("task_id", "")))
        summary = f"Task {task_id}: {row.get('error') or row.get('status') or 'output'}"
        blocks.append({"id": "output", "type": "markdown", "text": str(row.get("output") or "")})
        if row.get("error_reason"):
            blocks.append({"id": "error", "type": "text", "text": str(row["error_reason"])})
        child = _child_session(str(row.get("task_id") or args.get("task_id") or ""))
        if child:
            blocks.insert(
                0,
                {
                    "id": "child",
                    "type": "link",
                    "target": "session",
                    "uri": child,
                    "label": "Open child conversation",
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
                excerpt = str(display.get("answer_excerpt") or "").strip()
                if excerpt:
                    details.append(excerpt)
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
                    "label": task_id or "Recipient",
                }
            )
        action = row.get("action")
        action_summary = (
            {"queue": "Queued", "wake": "Follow-up started"}.get(action, summary)
            if isinstance(action, str)
            else summary
        )
        summary = str(row.get("error") or action_summary)
        if row.get("error"):
            blocks.append({"id": "error", "type": "text", "text": str(row["error"])})
        sent_message = str(args.get("message") or "").strip()
        if sent_message:
            blocks.append(
                {
                    "id": "message",
                    "type": "item",
                    "result_kind": "message",
                    "text": sent_message,
                }
            )
    elif declaration == "goal":
        summary = "Active goal" if row.get("active") else "No active goal"
        if row.get("condition"):
            blocks.append({"id": "condition", "type": "text", "text": str(row["condition"])})
        progress = []
        for field in ("iters_elapsed", "max_goal_iters"):
            if field in row:
                progress.append(f"{field.replace('_', ' ')}: {row[field]}")
        budget = row.get("budget_spent")
        if isinstance(budget, Mapping):
            for field in ("wallclock_s", "tokens"):
                if field in budget:
                    progress.append(f"{field}: {budget[field]}")
        if progress and (row.get("active") or row.get("condition") or row.get("iters_elapsed")):
            blocks.append({"id": "progress", "type": "text", "text": "\n".join(progress)})
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
            summary = f"Loop {row['loop_id']} stopped" if row.get("loop_id") else "No active loop"
        elif row.get("next_fire_at"):
            summary = (
                f"Next iteration scheduled for loop {row['loop_id']}"
                if row.get("loop_id")
                else "Next iteration scheduled"
            )
            details = [str(args.get("prompt") or ""), f"Next: {row['next_fire_at']}"]
            blocks.append({"id": "next", "type": "text", "text": "\n".join(filter(None, details))})
    elif declaration == "todos":
        summary = "" if row.get("todos") else "No tasks in this list"
        blocks.extend(
            {
                "id": f"todo-{index}",
                "type": "check",
                "state": todo.get("status", "pending"),
                "text": str(todo.get("content", "")),
            }
            for index, todo in enumerate(row.get("todos", []))
            if isinstance(todo, Mapping)
        )
    elif declaration == "schedules":
        text = "\n".join(
            "\n".join(
                str(value)
                for value in (
                    schedule.get("id"),
                    schedule.get("cron") or "One-shot",
                    schedule.get("timezone"),
                )
                if value
            )
            + f"\n{schedule.get('prompt', '')}\nNext: {schedule.get('next_fire_at', '')}"
            for schedule in row.get("schedules", [])
            if isinstance(schedule, Mapping)
        )
        if text:
            blocks.append({"id": "schedules", "type": "text", "text": text})
    elif declaration == "schedule_created":
        if row.get("schedule_id"):
            summary = (
                f"{'Recurring' if row.get('recurring') else 'One-shot'} schedule "
                f"{row['schedule_id']}"
            )
            details = [str(args.get("prompt") or "")]
            if row.get("cron"):
                details.append(f"Cron: {row['cron']}")
            details.extend(
                f"{label}: {row[key]}"
                for key, label in (("next_fire_at", "Next"), ("timezone", "Timezone"))
                if row.get(key)
            )
            blocks.append(
                {"id": "schedule", "type": "text", "text": "\n".join(filter(None, details))}
            )
    elif declaration == "schedule_deleted":
        # The declared message already names the schedule and actual removal state.
        # Repeating the identifier and boolean beneath it adds no result evidence.
        pass
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
            kind = "markdown" if row.get("representation") == "markdown" else "text"
            blocks.append({"id": "content", "type": kind, "text": content})
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
    subject = ""
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
                links[0]["label"] = str(args.get("task_id") or "Child task")
                summary = str(row.get("error") or row.get("error_reason") or "")
            elif declaration == "artifact":
                artifacts = row.get("artifacts", [])
                if len(artifacts) == 1 and artifacts[0].get("accepted") is True:
                    artifact = artifacts[0]
                    summary = ""
                    header_action = "Created" if artifact.get("created") else "Reused"
                    if artifact.get("version"):
                        summary = f"Version {artifact['version']}"
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
    explanations = {
        "escapes_root": (
            "The requested path is outside the active workspace. "
            "Artifacts can only reference files inside this workspace."
        ),
        "missing": "The requested artifact file does not exist.",
        "not_found": "The requested artifact file does not exist.",
    }
    explanation = explanations.get(
        reason, f"The artifact was rejected because {reason.replace('_', ' ')}."
    )
    detail = str(artifact.get("detail") or "").strip()
    return f"{explanation}\n{detail}" if detail else explanation


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
