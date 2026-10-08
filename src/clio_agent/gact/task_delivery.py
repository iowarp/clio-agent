"""Durable completion staging and the shared collection/delivery once guard."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.task_projection import TERMINAL, resolve_task, task_views
from clio_agent.tools.mcp_task_records import TaskKey


def enqueue_task_wake(app: Any, record: Any) -> None:
    """Wake an already-busy inbox after durable completion intent, without creating a turn."""
    from clio_agent.gact.loop_inbox import InboxEvent, inbox_for

    runner = getattr(app.state, "turn_runner", None)
    if runner is not None and record.session_id and runner.busy(record.session_id):
        inbox_for(app, record.session_id).put(
            InboxEvent(kind="child_completed", task_id=record.handle)
        )


def consume_task(app: Any, sid: str, handle: str) -> bool:
    """Claim terminal delivery once, without preventing later explicit result reads."""
    from clio_agent.gact.task_supervisor import task_supervisor

    with task_supervisor(app).delivery_lock:
        row = resolve_task(app, sid, handle)
        if row["effective_status"] not in TERMINAL:
            return False
        if row["task_kind"] == "Subagent":
            from clio_agent.gact.enrichment import consume_pending_agent_task_notifications

            return bool(consume_pending_agent_task_notifications(app, sid, [row["id"]]))
        store = app_task_store(app)
        record = store.get(TaskKey.from_wire(row["key"]))
        if record is None or record.consumed_at or not record.notify_pending:
            return False
        store.put(
            replace(
                record, consumed_at=datetime.now(timezone.utc).isoformat(), notify_pending=False
            )
        )
        return True


def pending_completions(app: Any, sid: str, *, limit: int = 8) -> tuple[str, list[str]]:
    """Stage bounded non-subagent completions; vetoed turns leave these pending."""
    rows = [
        r
        for r in task_views(app, sid)
        if r["task_kind"] != "Subagent"
        and r["owner"]["session_id"] == sid
        and r.get("notify_pending")
        and not r.get("consumed_at")
        and r["effective_status"] in TERMINAL
    ]
    selected = rows[:limit]
    if not selected:
        return "", []
    blocks = [completion_block(row) for row in selected]
    if len(rows) > limit:
        blocks.append(f"{len(rows) - limit} additional completions remain queued.")
    return "## Background task results\n\n" + "\n\n".join(blocks), [r["handle"] for r in selected]


def commit_staged_completions(state: Any) -> None:
    """Claim and recompose staged results so an intervening collection cannot duplicate delivery."""
    from clio_agent.gact.enrichment import consume_pending_agent_task_notifications
    from clio_agent.gact.task_supervisor import task_supervisor

    with task_supervisor(state.app).delivery_lock:
        blocks = consume_pending_agent_task_notifications(
            state.app, state.sid, state.pending_notification_task_ids
        )
        state.injections = [
            (source, text) for source, text in state.injections if source != "task_results"
        ]
        if blocks:
            state.injections.append(
                ("task_results", "## Background task results\n\n" + "\n\n".join(blocks))
            )


def completion_block(row: dict[str, Any]) -> str:
    """Render bounded results with the durable handle available for explicit readback."""
    text = json.dumps(row.get("result"), ensure_ascii=False)
    if len(text) > 6000:
        text = text[:6000] + "\n[Preview truncated; retrieve get_task_result(handle).]"
    return f"{row['task_kind']} {row['handle']}: {row['description']}\nStatus: {row['effective_status']}\n{text}"


def drain_task_completions(app: Any, sid: str) -> list[tuple[str, str]]:
    """Commit pending results at the existing safe model iteration boundary."""
    block, selected = pending_completions(app, sid)
    if not selected:
        return []
    delivered = []
    for handle in selected:
        row = resolve_task(app, sid, handle)
        if consume_task(app, sid, handle):
            delivered.append(completion_block(row))
    return (
        [("task_results", "## Background task results\n\n" + "\n\n".join(delivered))]
        if delivered
        else []
    )
