"""Apply marketplace mutations at a whole-turn boundary with durable receipts."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.gact.blueprint_activation import agent_blueprint_activation_metadata
from clio_agent.gact.blueprint_ledgers import write_json_atomic
from clio_agent.gact.blueprint_mutations import BLUEPRINT_MUTATION_LOCK
from clio_agent.gact.blueprint_operations import operation_directory
from clio_agent.gact.blueprint_revision import blueprint_revision_changed
from clio_agent.gact.blueprint_runtime_preparation import runtime_preparation
from clio_agent.gact.events import Event
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.session_warmup import release_session_fleet
from clio_agent.gact.storage.boundary import source_policy_change


def _reconcile_sessions(app: Any, result: dict[str, Any]) -> None:
    """Project exact applied provenance into sessions that use an updated root."""
    for row in result.get("installed", []):
        root = Path(str(row["root"])).resolve()
        for session in app.state.sessions.list():
            metadata = session.metadata or {}
            active = str(metadata.get("active_agent_blueprint_path") or "")
            if not active:
                active = str(metadata.get("active_agent_blueprint_definition_path") or "")
            if not active:
                continue
            active_path = Path(active).resolve()
            if active_path.name == "AGENT.md":
                active_path = active_path.parent
            if active_path != root:
                continue
            patch = agent_blueprint_activation_metadata(
                blueprint_wire=row,
                install_root=root,
                scope=str(row["scope"]),
                app=app,
                session_id=session.id,
            )
            patch["active_agent_blueprint_identity"] = str(row.get("identity") or "")
            app.state.sessions.update(session.id, metadata_patch=patch)


async def apply_blueprint_change(
    app: Any,
    change: Callable[[], dict[str, Any]],
    *,
    label: str,
    verify_runtime: bool = False,
    target: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Keep live turns coherent and keep an interrupted request from unguarding a write.

    New turn tasks queue in the runner while existing turns (and their children)
    drain. Waiting-session fleet holds are released before the guarded mutation;
    source-policy exclusion also covers concurrent MCP calls and warm-ups. The
    previous runtime files remain untouched if that boundary cannot be acquired.
    """
    target = dict(target or {})
    tasks = getattr(app.state, "blueprint_operation_tasks", None)
    if tasks is None:
        tasks = app.state.blueprint_operation_tasks = {}
        app.state.blueprint_active_operations = set()
    key = (label, *sorted(target.items())) if target else (uuid.uuid4().hex,)
    existing = tasks.get(key)
    if existing is not None:
        return await asyncio.shield(existing)
    operation_id = uuid.uuid4().hex
    receipt: dict[str, Any] = {
        "id": operation_id,
        "label": label,
        "status": "waiting_for_turns",
        "target": target,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = operation_directory() / f"{operation_id}.json"

    async def record(status: str, **extra: Any) -> None:
        receipt.update(status=status, **extra)
        await run_off_loop(lambda: write_json_atomic(receipt_path, receipt))
        app.state.bus.publish(
            Event(type="blueprint.operation.changed", session_id="", payload=dict(receipt))
        )

    def commit() -> dict[str, Any]:
        for session in app.state.sessions.list():
            release_session_fleet(session.id)
        with (
            source_policy_change(getattr(app.state, "agent", None)),
            BLUEPRINT_MUTATION_LOCK,
            runtime_preparation(app, enabled=verify_runtime) as prepared_runtime,
        ):
            result = change()
            if result.get("installed"):
                prepared_runtime.apply()
            _reconcile_sessions(app, result)
            return result

    async def run() -> dict[str, Any]:
        await record("waiting_for_turns")
        try:
            async with app.state.turn_runner.revision_gate.change():
                await record("preparing")
                result = await run_off_loop(commit)
                revision = blueprint_revision_changed(app)
                source = result.get("source") or {}
                status = source.get("status") if isinstance(source, dict) else None
                outcome = (
                    "failed"
                    if status == "error"
                    else "partial"
                    if status == "degraded"
                    else "applied"
                )
                await record(
                    outcome,
                    revision=revision,
                    result=result,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return {**result, "operation": dict(receipt)}
        except Exception as exc:
            await record(
                "failed", error=str(exc), finished_at=datetime.now(timezone.utc).isoformat()
            )
            raise

    task = asyncio.create_task(run())
    tasks[key] = task
    app.state.blueprint_active_operations.add(operation_id)

    def completed(_task: asyncio.Task[dict[str, Any]]) -> None:
        tasks.pop(key, None)
        app.state.blueprint_active_operations.discard(operation_id)

    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Python cannot cancel a running filesystem thread. Keep the exclusive
        # boundary until it has settled, even when the requesting client leaves.
        try:
            await asyncio.shield(task)
        finally:
            raise
