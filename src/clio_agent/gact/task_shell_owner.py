"""Application-owned background shell execution using the existing confined process."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from clio_agent.gact.context import active_react_scope
from clio_agent.gact.events import Event
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord
from clio_agent.tools.mcp_tasks import utcnow_iso
from clio_agent.tools.servers.shell_output import (
    StreamCapture,
    compose_output_fields,
    read_process_stream,
    shell_result_char_budget,
)

logger = logging.getLogger(__name__)


async def spawn_owned_shell(
    app: Any,
    sid: str,
    spawn: Coroutine[Any, Any, asyncio.subprocess.Process],
    captures: list[StreamCapture],
    *,
    command: str,
    cwd: Path,
    spill_root: Path,
    timeout: float,
    invocation_id: str,
) -> dict[str, Any]:
    """Finish custody after a spawn racing Stop, without replaying the command."""

    async def submit() -> dict[str, Any]:
        process = await spawn
        try:
            return accept_shell(
                app,
                sid,
                process,
                captures,
                command=command,
                cwd=cwd,
                spill_root=spill_root,
                timeout=timeout,
                invocation_id=invocation_id,
            )
        except Exception:
            # A failed ownership write is a submission failure. The command must
            # have no surviving execution owner before that failure is returned.
            from clio_agent.tools.servers.shell_server import _kill_process_tree

            await asyncio.to_thread(_kill_process_tree, process.pid)
            await process.wait()
            raise

    submission = asyncio.create_task(submit())
    try:
        return await asyncio.shield(submission)
    except asyncio.CancelledError:
        # Stop cancels this waiter, not the accepted command. Even when the
        # acknowledgement cannot reach the stopped turn, query_tasks discovers it.
        await asyncio.shield(submission)
        raise


def accept_shell(
    app: Any,
    sid: str,
    process: asyncio.subprocess.Process,
    captures: list[StreamCapture],
    *,
    command: str,
    cwd: Path,
    spill_root: Path,
    timeout: float,
    invocation_id: str,
) -> dict[str, Any]:
    """Persist ownership after real spawn succeeds and before returning acceptance."""
    from clio_agent.runtime.task_process_tree import TaskProcessTree

    handle = "task_" + uuid4().hex
    key = TaskKey("shell:local", sid, handle)
    tree = TaskProcessTree(process.pid)
    birth = tree.birth
    store = app_task_store(app)
    from clio_agent.tools.task_call_context import TASK_CALL

    description = TASK_CALL.get().description or command
    row = TaskRecord(
        key=key,
        handle=handle,
        kind="Shell",
        description=description,
        invocation_id=invocation_id,
        owner_agent=active_react_scope(),
        tool="shell_bash",
        status="working",
        created_at=utcnow_iso(),
        backend={
            "transport": "process",
            "pid": process.pid,
            "created_time": birth,
            "placement": "local",
            "cwd": str(cwd),
        },
    )
    try:
        if task_supervisor(app).closing:
            raise RuntimeError("Application task supervisor is shutting down")
        store.put(row)
        persisted = store.get(key)
        if persisted is None or persisted.holding_reason or persisted.handle != handle:
            raise RuntimeError("Shell task lacks durable ownership")
    except Exception:
        tree.terminate()
        tree.close()
        raise

    async def cancel() -> None:
        await asyncio.to_thread(tree.terminate)

    task_supervisor(app).supervise(
        handle,
        _run(
            app,
            key,
            process,
            captures,
            command=command,
            cwd=cwd,
            root=spill_root,
            timeout=timeout,
            cancel=cancel,
            tree=tree,
        ),
        cancel,
    )
    return {
        "accepted": True,
        "handle": handle,
        "kind": "Shell",
        "description": description,
        "status": "running",
    }


class ShellTaskProgress:
    """Forward bounded incremental output onto the task's existing session event channel."""

    def __init__(self, app: Any, key: TaskKey) -> None:
        self.app, self.key = app, key

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Persist the bounded console tail before publishing its correlated delta."""
        del total
        payload = json.loads(message or "{}")
        text = str(payload.get("text") or "")
        store = app_task_store(self.app)
        row = store.get(self.key)
        if row is None:
            raise RuntimeError("Shell task ownership disappeared")
        console = dict(row.backend.get("console") or {})
        console["tail"] = (str(console.get("tail") or "") + text)[-16000:]
        store.put(
            replace(
                row,
                backend={
                    **row.backend,
                    "console": console,
                    "progress": {"stream": payload.get("stream"), "bytes": progress},
                },
            )
        )
        self.app.state.bus.publish(
            Event(
                type="task.output",
                session_id=self.key.session_id or "",
                payload={"handle": row.handle, "invocation_id": row.invocation_id, **payload},
            )
        )


async def _run(
    app: Any,
    key: TaskKey,
    process: asyncio.subprocess.Process,
    captures: list[StreamCapture],
    *,
    command: str,
    cwd: Path,
    root: Path,
    timeout: float,
    cancel: Any,
    tree: Any,
) -> None:
    store = app_task_store(app)
    reporter = ShellTaskProgress(app, key)
    readers = [
        asyncio.create_task(read_process_stream(pipe, capture=capture, ctx=reporter))
        for pipe, capture in zip((process.stdout, process.stderr), captures, strict=True)
    ]
    timed_out, interrupted = False, False
    error = ""

    async def settle() -> None:
        await process.wait()
        while await asyncio.to_thread(tree.active):
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(settle(), timeout=timeout or None)
    except TimeoutError:
        timed_out = True
        await cancel()
        await settle()
    except asyncio.CancelledError:
        interrupted = True
        await cancel()
        await settle()
    except Exception as exc:
        logger.exception("Owned shell task failed handle=%s", key.task_id)
        error = str(exc)
        await cancel()
        await settle()
    finally:
        reader_results = await asyncio.gather(*readers, return_exceptions=True)
        errors = [str(value) for value in reader_results if isinstance(value, Exception)]
        if errors:
            error = "; ".join([error, *errors]).strip("; ")
        tree.close()
    row = store.get(key)
    if row is None:
        raise RuntimeError("Shell task mapping disappeared at settlement")
    base = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "timeout_s": timeout,
        "error": error or None,
    }
    result = compose_output_fields(base, captures, root=root, budget=shell_result_char_budget())
    status = (
        "interrupted"
        if interrupted
        else "cancelled"
        if row.cancel_requested
        else "failed"
        if error or timed_out or process.returncode
        else "completed"
    )
    store.put(
        replace(
            row,
            status=status,
            effective_status=status,
            result=result,
            notify_pending=not bool(row.consumed_at),
            connection_freshness="settled",
        )
    )
