"""Uniform runs registry projected over the two existing task stores (#1127).

There is deliberately no registry object and no fifth store. Every read re-sources
local/relay-backed child runs from ``AgentTaskRegistry`` and reconnectable relay job
handles from the installed ``TaskRecordStore``. Matching ids are de-duplicated in
favor of the richer AgentTask row seeded by ``RelayExpertInvoker``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_tasks import AgentTask, persist_agent_task
from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, TaskRecord, resolve_store

if TYPE_CHECKING:
    from fastapi import FastAPI

_RELAY_LIVE_STATES = {
    "working": "running",
    "input_required": "input_required",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _agent_run(task: AgentTask) -> dict[str, Any]:
    """Project one session-backed AgentTask as a human-facing run handle."""

    expert_id = str(task.agent_ref.get("expert_id") or "agent")
    handle_id = task.handle_id or task.task_id
    run_label = task.run_label or f"{expert_id} #{task.run_index + 1}"
    live_state = task.live_state or task.status
    placement = task.placement or "local"
    host = task.host or (placement.split(":", 1)[1] if placement.startswith("relay:") else "local")
    return {
        "handle_id": handle_id,
        "task_id": task.task_id,
        "run_label": run_label,
        "live_state": live_state,
        "status": task.status,
        "host": host,
        "placement": placement,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "detached": task.detached,
        "source": "agent_task",
        "ticker": {
            "state": live_state,
            "updated_at": task.updated_at,
            "path": f"/v1/agent-tasks/{task.task_id}/live",
        },
    }


def _relay_run(record: TaskRecord) -> dict[str, Any]:
    """Project one durable MCP/relay task record not mirrored by an AgentTask.

    #1205 review (4th round) — honesty: ``placement``/``source`` used to be a
    blanket ``"relay:<...>"``/``"relay_job"`` claim for EVERY non-agent-task
    record, including a plain ``jarvis_run`` call against the generic #1115
    ``ClioTasksClientExtension`` leaf, whose backend locator
    (``mcp_task_extension.py::_locator_for``) carries NO ``cluster`` key at
    all — that record isn't a relay job. Only a record whose backend locator
    actually carries relay's own ``cluster`` key
    (``relay_invoker_runtime.py``'s ``RelayExpertInvoker`` path,
    ``relay_submit_agent``) is genuinely relay-placed; everything else
    gets an honest ``mcp:<server_id>`` / ``"mcp_task"`` instead of a false
    relay claim. Server-authored display semantics must be true — the UI
    renders them verbatim. ``updated_at`` is now wired through from the real,
    stamped field (``TaskRecord.updated_at``, #1205 2nd/3rd round) instead of
    a hardcoded empty string.

    #1236 (clio-relay#265's client half, owner ruling 2026-08-20): ``status``
    here is now :attr:`TaskRecord.display_status` — the protocol-truth-derived
    field, primary because a run card reading the raw SEP-2663 wire status
    alone would show a delivered-error task as bare "completed" ("completed is
    a terrible status indicator"). The raw wire value is never discarded: it
    rides alongside as ``protocol_status``, and ``status_reason`` carries the
    extracted error text when the two diverge.
    """

    cluster = record.backend.get("cluster")
    is_relay = isinstance(cluster, str) and bool(cluster)
    display_status = record.display_status
    live_state = _RELAY_LIVE_STATES.get(display_status, display_status)
    return {
        "handle_id": record.task_id,
        "task_id": record.task_id,
        "run_label": record.tool or f"task {record.task_id}",
        "live_state": live_state,
        "status": display_status,
        "protocol_status": record.status,
        "status_reason": record.effective_status_reason,
        "host": cluster if is_relay else record.key.server_id,
        "placement": f"relay:{cluster}" if is_relay else f"mcp:{record.key.server_id}",
        "parent_session_id": record.session_id or "",
        "child_session_id": "",
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "detached": record.lease_owner is None,
        "source": "relay_job" if is_relay else "mcp_task",
        "ticker": {
            "state": live_state,
            "updated_at": record.updated_at,
            "path": "",
        },
    }


def project_runs(app: "FastAPI") -> list[dict[str, Any]]:
    """List local and relay runs uniformly, newest-created first.

    AgentTask ids are retained even when dismissed so a mirrored relay handle does
    not reappear through the second projection source. #1205 review item 3: widened
    the SAME way ``dismiss_run`` was widened — ANY non-agent-task ``TaskRecord``
    (``jarvis_run`` etc.), not only the relay-agent-mirroring
    ``relay_submit_agent`` records this originally covered — so a RETAINED
    settled mcp-task (#1205 2nd round: kept until an explicit dismiss, never
    auto-dropped at settle) is actually reachable through the SAME listing its
    dismiss control (``POST /v1/runs/{handle_id}/dismiss``) targets. Without this
    half, retention is an unbounded, unclearable accumulation in ``sessions.json``:
    a settled record nobody can ever see or dismiss through this surface.
    """

    tasks = app.state.agent_task_registry.snapshot()
    agent_ids = {task.task_id for task in tasks}
    rows = [_agent_run(task) for task in tasks if not task.dismissed]
    rows.extend(
        _relay_run(record)
        for record in resolve_store(None).list()
        if record.task_id not in agent_ids
    )
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def detach_run(app: "FastAPI", handle_id: str) -> dict[str, Any] | None:
    """Detach a run without cancelling it, using only its existing authoritative store.

    #1205 review (4th round): widened the SAME way ``dismiss_run``/``project_runs``
    were — ANY non-agent-task ``TaskRecord``, not only the relay-agent-mirroring
    ``relay_submit_agent`` records this originally covered. Without this,
    ``project_runs`` lists a ``jarvis_run`` row (the prior round's widening) that
    this route 404s on — a row the listing shows but cannot act on, which is its
    own dishonesty. UNLIKE ``dismiss_run``, this carries NO terminal-state guard:
    detach is explicitly "without cancelling" (matches the ``AgentTask`` branch
    above, which detaches a still-RUNNING task just fine) — a live task can be
    detached too. Composite-key precision (mirrors ``dismiss_run``): resolves to
    AT MOST ONE matching record.
    """

    task = app.state.agent_task_registry.get(handle_id)
    if task is not None:
        if not task.detached:
            task = replace(task, detached=True)
            persist_agent_task(app, task)
        return _agent_run(task)
    match = next(
        (record for record in resolve_store(None).list() if record.task_id == handle_id),
        None,
    )
    if match is None:
        return None
    # A relay/MCP record with no active lease is already detached from a driver;
    # detaching never drops or cancels its reconnect handle. TaskRecord carries no
    # persistent "detached" field, so — same as before this widening — this stays
    # a display-only affordance in the response, never written to the store.
    return {**_relay_run(match), "detached": True}


def dismiss_run(app: "FastAPI", handle_id: str) -> bool:
    """Hide one run while leaving execution untouched.

    An ``AgentTask``-backed run is hidden via its ``dismissed`` field — never
    dropped, so ``for_parent``/``project_runs`` keep returning the row (a
    dismissed run is hidden by the CLIENT's own filter, not erased server-side).
    A durable MCP/relay ``TaskRecord`` has no such field: #1205's retention
    design (2nd round) keeps a settled record in the store with its terminal
    status until this explicit action, so dismissing one is the ONE way to make
    it stop appearing — this call drops it for real. Matches ANY tool now, not
    only the relay-agent-mirroring ``relay_submit_agent`` records this
    originally covered — the session-scoped async-processes tray (#1205)
    surfaces every non-agent-task record (``jarvis_run`` etc.), not just that one.

    Two invariants #1205 review (3rd round) holds this to:

    * **Terminality guard (BLOCKING).** A live (non-terminal) ``TaskRecord`` is
      NEVER dropped here — dropping it would delete the only durable local handle
      to a still-running remote task (the exact crash-recovery guarantee
      ``mcp_task_store.py``'s own module contract exists to protect; the old
      tool-name filter only incidentally shielded this by accident, not by
      design). A dismiss request against a non-terminal task is refused
      (``False``), same shape as "no match" — it never partially acts.
    * **Composite-key precision (BLOCKING).** ``handle_id`` is a bare task id, but
      the durable identity is the COMPOSITE ``(server_id, session_id, task_id)``
      — two different backends can legitimately mint the same task id
      (``mcp_task_records.py``'s own module contract; ``cancel_task``'s docstring
      states the identical invariant and ``test_cancel_stamps_only_the_named_identity``
      guards it there). This resolves to at most ONE matching record and drops
      only THAT record's own composite key — never a blanket sweep of every
      record merely sharing the bare id, which would delete an unrelated
      backend's live task as collateral damage.
    """

    task = app.state.agent_task_registry.get(handle_id)
    if task is not None:
        if not task.dismissed:
            persist_agent_task(app, replace(task, dismissed=True))
        return True
    store = resolve_store(None)
    match = next((record for record in store.list() if record.task_id == handle_id), None)
    if match is None or match.status not in TERMINAL_TASK_STATES:
        return False
    store.drop(match.key)
    return True
