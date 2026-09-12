"""Declared native-tool presenters, isolated from execution and observation.

``native_presentation`` is the dispatch entrypoint; the task/schedule/memory
declaration families are owned by sibling modules (#1333 ratchet payment,
``native_presenters_tasks.py`` / ``native_presenters_schedule.py`` /
``native_presenters_memory.py``) so this module stays under the file-size cap.
Only ``build_wait_tool``, ``waiting_presentation``, ``validate_declaration``, and
``native_presentation`` are imported elsewhere — every other name here is either a
private helper for the declarations kept in this module (``text``, ``model_catalog``,
``fields:``) or a re-export kept for monkeypatch compatibility (see
``native_presenters_memory.py``'s module docstring).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

# Re-exported (not just used) so ``native_presenters._resource_link`` /
# ``native_presenters._session_link`` remain valid monkeypatch targets for
# tests/test_gact/test_native_presentation_density.py even though the real
# definitions now live in native_presenters_memory.py.
from clio_agent.gact.agents.native_presenters_memory import (  # noqa: F401
    _resource_link,
    _session_link,
)

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


def _visible_skill_body(result: str, skill_id: str) -> str:
    """Remove the model-facing skill identity already owned by the tool header."""

    lines = result.splitlines()
    if lines and lines[0].strip() == f"# Skill: {skill_id}":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines)
    return result


def _model_catalog_presentation(row: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Render one row per provider probe result (added/unchanged/removed/rejected)."""

    blocks: list[dict[str, Any]] = []
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
    return summary, blocks


def _fields_presentation(declaration: str, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Render the ``fields:a,b,c`` generic declaration as one text block per field."""

    blocks: list[dict[str, Any]] = []
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
    return blocks


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
        if args.get("skill_id"):
            subject = "skill-name"
            blocks.insert(0, {"id": subject, "type": "text", "text": str(args["skill_id"])})
            summary = ""
    elif declaration in {"task_output", "tasks", "wait", "message"}:
        from clio_agent.gact.agents.native_presenters_tasks import present_task_family

        summary, blocks, header_action, presentation_status, subject = present_task_family(
            declaration, args, result, row, summary
        )
    elif declaration in {
        "goal",
        "loop",
        "todos",
        "schedules",
        "schedule_created",
        "schedule_deleted",
    }:
        from clio_agent.gact.agents.native_presenters_schedule import present_schedule_family

        summary, blocks, header_action, presentation_status, subject = present_schedule_family(
            declaration, args, result, row, summary
        )
    elif declaration == "model_catalog":
        summary, blocks = _model_catalog_presentation(row)
    elif declaration in {"memory", "resource", "artifact"}:
        from clio_agent.gact.agents.native_presenters_memory import present_memory_family

        summary, blocks, header_action, presentation_status, subject = present_memory_family(
            declaration, args, result, row, summary
        )
    elif declaration.startswith("fields:"):
        blocks = _fields_presentation(declaration, row)
    elif declaration != "specialized":
        raise ValueError(f"Unknown native presentation declaration: {declaration}")

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
