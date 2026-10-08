"""Shared agent and UI task controls; execution stays with each existing owner."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.task_projection import KINDS, TERMINAL, public_task, resolve_task, task_views


def _scope() -> tuple[Any, str]:
    app, sid = context.active_app(), context.active_session_id()
    if app is None or not sid or app.state.sessions.get(sid) is None:
        raise ValueError("Task controls require an active CLIO session")
    return app, sid


def handles(tasks: str | list[str]) -> list[str]:
    """Validate a handle or a nonempty handle list, preserving request order."""
    values = [tasks] if isinstance(tasks, str) else tasks
    if (
        not values
        or not isinstance(values, list)
        or any(not isinstance(v, str) or not v for v in values)
    ):
        raise ValueError("tasks must contain one or more handles")
    return list(dict.fromkeys(values))


def query_snapshot(
    app: Any,
    sid: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    handle: str | None = None,
    tool: str | None = None,
    agent: str | None = None,
    active_only: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return an authorized, filtered keyset page from the existing projection."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    filters = [sid, kind, status, handle, tool, agent, active_only]
    fingerprint = hashlib.sha256(json.dumps(filters).encode()).hexdigest()
    after = None
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            if token["scope"] != fingerprint:
                raise ValueError("cursor scope changed")
            after = tuple(token["after"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid task cursor") from exc
    rows = [
        row
        for row in task_views(app, sid)
        if (kind is None or row["task_kind"] == kind)
        and (status is None or row["effective_status"] == status)
        and (handle is None or row["handle"] == handle)
        and (tool is None or row.get("tool") == tool)
        and (
            agent is None
            or agent in (row.get("owner_agent"), row.get("agent_ref", {}).get("expert_id"))
        )
        and (not active_only or row["effective_status"] not in TERMINAL)
    ]

    def key(row: dict[str, Any]) -> tuple[str, str]:
        return row.get("created_at", ""), row["handle"]

    rows.sort(key=key, reverse=True)
    total = len(rows)
    page = [row for row in rows if after is None or key(row) < after][: limit + 1]
    next_cursor = None
    if len(page) > limit:
        page = page[:limit]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"scope": fingerprint, "after": key(page[-1])}).encode()
        ).decode()
    return {"tasks": [public_task(row) for row in page], "total": total, "cursor": next_cursor}


def query_tasks(
    kind: str | None = None,
    status: str | None = None,
    handle: str | None = None,
    tool: str | None = None,
    agent: str | None = None,
    active_only: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List task handles, assignments and status. query_tasks('Subagent') filters children."""
    app, sid = _scope()
    return query_snapshot(
        app,
        sid,
        kind=kind,
        status=status,
        handle=handle,
        tool=tool,
        agent=agent,
        active_only=active_only,
        cursor=cursor,
        limit=limit,
    )


def _selected(
    app: Any, sid: str, tasks: str | list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows, errors = [], []
    for handle in handles(tasks):
        try:
            rows.append(resolve_task(app, sid, handle))
        except ValueError as exc:
            errors.append({"handle": handle, "error": str(exc)})
    return rows, errors


def collect_result(app: Any, sid: str, row: dict[str, Any]) -> dict[str, Any]:
    """Read a stored result and atomically share the notification delivery guard."""
    from clio_agent.gact.task_delivery import consume_task

    if row["effective_status"] not in TERMINAL:
        return {"handle": row["handle"], "status": row["effective_status"], "pending": True}
    consume_task(app, sid, row["handle"])
    result = row.get("result")
    if row["task_kind"] == "Subagent":
        from clio_agent.gact.agents.agent_task_output_digest import digested_model_row
        from clio_agent.gact.agents.spawn_completion import completion_payload

        task = app.state.agent_task_registry.get(row["id"])
        result = digested_model_row(completion_payload(app, task), task)
    from clio_agent.tools.mcp_result_projection import bounded_model_tool_result

    rendered = bounded_model_tool_result(json.dumps(result, ensure_ascii=False, default=str))
    try:
        result = json.loads(rendered)
    except json.JSONDecodeError:
        result = rendered
    return {
        "handle": row["handle"],
        "kind": row["task_kind"],
        "status": row["effective_status"],
        "result": result,
        "result_reference": row["result_reference"],
    }


def get_task_result(handle: str) -> dict[str, Any]:
    """Read a terminal result again; explicit reads remain possible after delivery."""
    app, sid = _scope()
    return collect_result(app, sid, resolve_task(app, sid, handle))


def _deadline(timeout_s: float | None) -> float | None:
    if timeout_s is not None and (timeout_s < 0 or not float(timeout_s) < float("inf")):
        raise ValueError("timeout_s must be finite and nonnegative")
    return None if timeout_s is None else time.monotonic() + timeout_s


def _wait_change(app: Any, sid: str, version: int, deadline: float | None) -> None:
    if sid in app.state.cancel_flags:
        raise InterruptedError("Turn stopped; accepted tasks continue")
    left = 0.25 if deadline is None else min(0.25, max(0, deadline - time.monotonic()))
    app.state.bus.wait_for_session_events(
        [sid, *[r["owner"]["session_id"] for r in task_views(app, sid)]],
        after_event_id=version,
        timeout_s=left,
    )


def wait_tasks(
    tasks: str | list[str], return_when: str = "all", timeout_s: float | None = None
) -> dict[str, Any]:
    """Wait for any/all mixed tasks. Default is unbounded; expiry or Stop ends only the wait."""
    if return_when not in {"any", "all"}:
        raise ValueError("return_when must be any or all")
    app, sid = _scope()
    deadline = _deadline(timeout_s)
    while True:
        version = app.state.bus.highest_event_id
        rows, errors = _selected(app, sid, tasks)
        settled = [r for r in rows if r["effective_status"] in TERMINAL]
        pending = [r["handle"] for r in rows if r["effective_status"] not in TERMINAL]
        expired = deadline is not None and time.monotonic() >= deadline
        if not pending or expired or (return_when == "any" and (settled or errors)):
            return {
                "results": [collect_result(app, sid, r) for r in settled],
                "errors": errors,
                "pending": pending,
                "timed_out": bool(pending and expired),
            }
        _wait_change(app, sid, version, deadline)


def observe_tasks(
    tasks: str | list[str],
    cursor: str | int | None = None,
    pattern: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Read incremental output without collecting completion; pattern waits for output or settlement."""
    app, sid = _scope()
    deadline = _deadline(timeout_s)
    regex = re.compile(pattern) if pattern is not None else None
    start, recovered = _observe_cursor(app, cursor)
    if recovered:
        start = 0
    while True:
        version = app.state.bus.highest_event_id
        rows, errors = _selected(app, sid, tasks)
        events = []
        for row in rows:
            session_ids = [row["owner"]["session_id"]]
            if row["task_kind"] == "Subagent":
                session_ids.append(row["child_session_id"])
            for owner in session_ids:
                for event in app.state.bus.session_events_since(owner, cursor=start + 1):
                    if not start < event.id <= version:
                        continue
                    key = event.payload.get("key")
                    selected = (
                        owner == row.get("child_session_id")
                        or event.payload.get("handle") == row["handle"]
                        or (
                            key == row.get("key")
                            if key
                            else event.payload.get("task_id") == row["id"]
                            and sum(r["id"] == row["id"] for r in task_views(app, sid)) == 1
                        )
                    )
                    if selected:
                        events.append(event)
        unique = {e.id: _observation(e) for e in events}
        all_output = sorted(unique.values(), key=lambda row: row["id"])
        output = all_output[:40]
        matched = regex is not None and any(
            regex.search(json.dumps(row)[:4000]) for row in all_output
        )
        settled = any(r["effective_status"] in TERMINAL for r in rows)
        expired = deadline is not None and time.monotonic() >= deadline
        if regex is None or errors or settled or expired or matched:
            return {
                "tasks": [public_task(row) for row in rows],
                "events": output,
                "errors": errors,
                "cursor": base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "bus": app.state.bus.task_cursor_epoch,
                            "after": output[-1]["id"] if len(all_output) > len(output) else version,
                        }
                    ).encode()
                ).decode(),
                "matched": bool(matched),
                "cursor_recovered": recovered,
                "timed_out": expired and not settled,
            }
        _wait_change(app, sid, version, deadline)


def _observe_cursor(app: Any, cursor: str | int | None) -> tuple[int, bool]:
    if isinstance(cursor, str):
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            if token["bus"] != app.state.bus.task_cursor_epoch:
                return 0, True
            after = int(token["after"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid observation cursor") from exc
    else:
        after = int(cursor or 0)
    if after < 0:
        raise ValueError("Observation cursor cannot be negative")
    return after, after > app.state.bus.highest_event_id


def _observation(event: Any) -> dict[str, Any]:
    from clio_agent.gact.agents.observe_runtime import curate_event

    curated = curate_event(event)
    payload = curated or {
        name: value
        for name, value in event.payload.items()
        if name
        in {
            "handle",
            "task_id",
            "status",
            "effective_status",
            "text",
            "message",
            "stream",
            "progress",
        }
    }
    if curated is None:
        backend = event.payload.get("backend") or {}
        if isinstance(backend, dict):
            if isinstance(backend.get("progress"), dict):
                payload["progress"] = backend["progress"]
            console = backend.get("console") or {}
            if isinstance(console, dict) and isinstance(console.get("tail"), str):
                payload["text"] = console["tail"][-4000:]
    bounded = {
        key: value[:4000] if isinstance(value, str) else value for key, value in payload.items()
    }
    return {"id": event.id, "type": event.type, "payload": bounded}


def cancel_selected(app: Any, sid: str, tasks: str | list[str]) -> dict[str, Any]:
    """Request cancellation through each task's owner, never infer settlement from an ACK."""
    from clio_agent.gact.task_supervisor import task_supervisor

    rows, errors = _selected(app, sid, tasks)
    results = []
    for row in rows:
        try:
            requested = task_supervisor(app).request_cancel(row)
            results.append(
                {
                    "handle": row["handle"],
                    "cancellation_requested": requested,
                    "status": row["effective_status"],
                }
            )
        except (ValueError, RuntimeError) as exc:
            errors.append({"handle": row["handle"], "error": str(exc)})
    return {"results": results, "errors": errors}


def cancel_tasks(tasks: str | list[str]) -> dict[str, Any]:
    """Cancel selected tasks. Subagent cancellation also cancels its entire task subtree."""
    return cancel_selected(*_scope(), tasks)
