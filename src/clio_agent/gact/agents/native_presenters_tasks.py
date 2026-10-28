"""Present authorized common-task observations without reconstructing child registries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.agents.native_presenters import _record
from clio_agent.gact.task_wait_presentation import wait_presentation


def task_submission_presentation(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Present accepted native work without claiming it has completed."""
    handle = result.get("handle")
    if result.get("accepted") is not True or not isinstance(handle, str) or not handle.strip():
        return None
    kind = result.get("kind")
    if kind not in ("Indexing", "Download") or result.get("acceptance") or result.get("error"):
        return None
    label = kind
    status = result.get("status")
    completed = status == "completed"
    failed = isinstance(status, str) and status in {
        "failed",
        "cancelled",
        "interrupted",
        "denied",
        "error",
    }
    description = str(result.get("description") or "").strip()
    identity = description or str(kind)
    block = {
        "id": "task-submission",
        "type": "text",
        "label": identity,
        "text": label,
        "detail": f"Task handle: {handle}",
        "status": str(status or "pending") if completed or failed else "pending",
        "result_kind": "completion" if completed or failed else "snapshot",
    }
    if failed:
        block["severity"] = "error"
    presentation: dict[str, Any] = {
        "summary": f"{label} {'completed' if completed else 'unsuccessful' if failed else 'requested'}",
        "blocks": [block],
    }
    if failed:
        presentation["status"] = "degraded"
    return presentation


def _flow_label(value: Any) -> str:
    """Humanize a flow identifier present in the invocation or result receipt."""
    flow = str(value or "").strip()
    return flow[:1].upper() + flow[1:].replace("-", " ").replace("_", " ") if flow else ""


def _label(row: Mapping[str, Any], flow: str = "") -> str:
    """Use only the authorized response, never another session lookup."""
    return str(
        _flow_label(row.get("flow") or flow)
        or row.get("label")
        or row.get("description")
        or row.get("kind")
        or "Task"
    )


def _detail(value: Any) -> str:
    """Bound the display; complete output remains in the tool observation."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping) and isinstance(value.get("text"), str):
        text = value["text"]
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= 1800 else text[:1740].rstrip() + "\nMore in technical details"


def _task_blocks(
    row: Mapping[str, Any], index: int, *, result_body: bool = False, flow: str = ""
) -> list[dict[str, Any]]:
    """Render a single common snapshot or independent control outcome."""
    label = _label(row, flow)
    status = str(row.get("activity") or row.get("status") or "")
    error = row.get("error")
    blocks: list[dict[str, Any]] = []
    result = row.get("result")
    if result_body and result is None and isinstance(row.get("output"), str):
        result = row["output"]  # Shipped getter alias; no separate result lookup.
    if result_body and result is not None:
        # The shared presentation projector owns paging and complete-text references.
        blocks.append(
            {
                "id": f"task-{index}-result",
                "type": "text",
                "label": "Result",
                "text": result
                if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False, indent=2, default=str),
            }
        )
    owner = row.get("owner")
    session = owner.get("session_id") if isinstance(owner, Mapping) else row.get("session_id")
    if row.get("kind") in {"Session", "Subagent"} and isinstance(session, str) and session:
        blocks.append(
            {
                "id": f"task-{index}-identity",
                "type": "link",
                "target": "session",
                "uri": session,
                "label": label,
                "detail": str(row.get("handle") or ""),
            }
        )
    else:
        blocks.append(
            {
                "id": f"task-{index}-identity",
                "type": "text",
                "label": label,
                "text": str(row.get("kind") or "Task"),
                "detail": str(row.get("handle") or ""),
            }
        )
    description = str(row.get("description") or "")
    if description:
        blocks.append(
            {
                "id": f"task-{index}-assignment",
                "type": "text",
                "label": "Assignment",
                "text": description,
            }
        )
    progress = row.get("progress")
    progress_text = ""
    if isinstance(progress, Mapping):
        progress_text = str(progress.get("message") or "")
        done, total = progress.get("completed"), progress.get("total")
        if done is not None:
            count = " ".join(
                filter(
                    None,
                    (
                        str(done) + (f" / {total}" if total is not None else ""),
                        str(progress.get("unit") or ""),
                    ),
                )
            )
            progress_text = f"{progress_text} ({count})" if progress_text else count
    text = str(error) if error else " — ".join(filter(None, (status, progress_text)))
    if text:
        block = {
            "id": f"task-{index}-status",
            "type": "text",
            "label": "Status",
            "text": text,
        }
        if error or row.get("status") in {"failed", "interrupted", "task_state_uncertain"}:
            block["severity"] = "error"
        blocks.append(block)
    chunks = row.get("output")
    if isinstance(chunks, list):
        text = "\n".join(
            _detail(chunk["output"])
            for chunk in chunks
            if isinstance(chunk, Mapping) and chunk.get("output") is not None
        )
        if text:
            if row.get("output_truncated"):
                text += "\nMore output is available."
            blocks.append(
                {
                    "id": f"task-{index}-progress",
                    "type": "text",
                    "label": "New output",
                    "text": _detail(text),
                }
            )
        errors = [
            str(chunk["error"])
            for chunk in chunks
            if isinstance(chunk, Mapping) and chunk.get("error")
        ]
        if errors:
            blocks.append(
                {
                    "id": f"task-{index}-output-error",
                    "type": "text",
                    "label": "Output unavailable",
                    "severity": "warning",
                    "text": _detail("\n".join(errors)),
                }
            )
    if (row.get("result_reference") and result is None) or (
        not result_body and row.get("result") is not None
    ):
        blocks.append(
            {
                "id": f"task-{index}-reference",
                "type": "text",
                "text": "Full result is available through get_task_result.",
            }
        )
    return blocks


def present_task_family(
    declaration: str,
    args: Mapping[str, Any],
    row: Mapping[str, Any],
    result: Any,
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    """Present shared task controls, preserving independent outcomes and pending work."""
    payload = _record(result) or dict(row)
    if payload.get("restart_requested") is True and payload.get("pending") is True:
        handle = payload.get("handle")
        block: dict[str, Any] = {
            "id": "task-restart",
            "type": "text",
            "label": "Task restart",
            "text": "Restart requested; waiting for task restart",
            "status": "pending",
            "result_kind": "snapshot",
        }
        if isinstance(handle, str) and handle:
            block["detail"] = f"Task handle: {handle}"
        return (
            "Restart requested; waiting for task restart",
            [block],
            "",
            "",
            "task-restart",
        )
    if (submission := task_submission_presentation(payload)) is not None:
        return (
            submission["summary"],
            submission["blocks"],
            "",
            submission.get("status", ""),
            "task-submission",
        )
    if declaration == "wait":
        presentation = wait_presentation(args, payload)
        return (
            str(presentation.get("summary") or "Waiting for tasks"),
            list(presentation.get("blocks") or []),
            "Wait",
            str(presentation.get("status") or ""),
            str(presentation.get("subject") or ""),
        )
    if declaration == "message":
        error = payload.get("error")
        session = (
            payload.get("session_id")
            or payload.get("target_session")
            or payload.get("target_session_id")
        )
        accepted = payload.get("accepted", not error)
        blocks: list[dict[str, Any]] = []
        if accepted and isinstance(args.get("message"), str) and args["message"]:
            blocks.append({"id": "message", "type": "text", "text": _detail(args["message"])})
        if accepted and isinstance(session, str) and session:
            blocks.append(
                {
                    "id": "recipient",
                    "type": "link",
                    "target": "session",
                    "uri": session,
                    "label": "Recipient",
                }
            )
        if error:
            blocks.append(
                {"id": "message-error", "type": "text", "severity": "error", "text": str(error)}
            )
        return (
            "Message accepted" if accepted else "Message not accepted",
            blocks,
            "",
            "failed" if error else "",
            "recipient" if accepted and isinstance(session, str) and session else "",
        )
    values = payload.get("tasks", payload.get("outcomes"))
    selected = (
        [value for value in values if isinstance(value, Mapping)]
        if isinstance(values, list)
        else [payload]
    )
    blocks = []
    subjects: list[str] = []
    for index, selected_row in enumerate(selected):
        row_blocks = _task_blocks(
            selected_row,
            index,
            result_body=declaration == "task_output",
            flow=str(args.get("flow") or ""),
        )
        blocks.extend(row_blocks)
        subjects.extend(
            block["id"] for block in row_blocks if block.get("id", "").endswith("-identity")
        )
    pending = payload.get("pending")
    if isinstance(pending, list) and pending:
        labels_by_handle = {
            str(task.get("handle")): _label(task, str(args.get("flow") or ""))
            for task in selected
            if task.get("handle")
        }
        pending_details = [
            f"{labels_by_handle.get(str(handle), 'Task')} ({handle})" for handle in pending
        ]
        blocks.append(
            {
                "id": "pending",
                "type": "text",
                "label": "Still running",
                "text": ", ".join(pending_details),
            }
        )
    error = payload.get("error")
    if declaration == "task_output":
        collected = sum(
            task.get("result") is not None or isinstance(task.get("output"), str)
            for task in selected
        )
        summary = f"{collected} {'result' if collected == 1 else 'results'} collected"
        if collected == 0:
            summary = "No result body recorded"
    elif payload.get("accepted") is True and (payload.get("flow") or args.get("flow")):
        summary = "Flow accepted"
    else:
        count = len(selected)
        summary = f"{count} {'task' if count == 1 else 'tasks'}"
    if isinstance(pending, list) and pending:
        summary += f", {len(pending)} pending"
    subject = subjects[0] if len(subjects) == 1 else ""
    return summary, blocks, "", "failed" if error else "", subject
