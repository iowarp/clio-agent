"""What the task ledger keeps of a view's own calls, against a real MCP session and Core.

MC-F3: a view's call to a tool the model never sees (``visibility: ["app"]``) stored its
whole reply (the PDF viewer's 512 KB parts) and arguments (page images) in the ledger and
filled clio-core. Its record now holds a size and a hash; the view still gets the reply.
MC-F2: a view's long-poll cut off by its page going away opened an attention episode and
woke the agent; it now ends terminal with ``settlement: unknown`` and wakes no one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ToolAnnotations

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context, view_call_cancel
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.view_call_ledger import VIEW_ORIGIN
from clio_agent.tools import call_origin, view_only_results
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_external_read_only_admission import _environment

CHUNK = "A" * 200_000
VIEW_ONLY = {"_meta": {"ui": {"visibility": ["app"]}}}
MODEL_SEES = {"_meta": {"ui": {"visibility": ["model", "app"]}}}


def _server(started: threading.Event, release: threading.Event) -> FastMCP:
    server = FastMCP("viewer")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False))
    async def read_bytes(offset: int, page_image: str = "") -> dict[str, Any]:
        return {"offset": offset, "bytes": CHUNK, "echo": len(page_image)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False))
    async def poll() -> dict[str, str]:
        started.set()
        await asyncio.to_thread(release.wait, 30)
        return {"commands": "none"}

    return server


async def _view_call(
    app: Any,
    root: Any,
    external: ExternalTasks,
    client: Client,
    tool: str,
    arguments: dict[str, Any],
    definition: dict[str, Any],
    *,
    cancel: threading.Event | None = None,
) -> Any:
    with ExitStack() as stack:
        stack.enter_context(call_origin.scope("view"))
        stack.enter_context(view_only_results.scope(tool, definition))
        if cancel is not None:
            stack.enter_context(view_call_cancel.bound(cancel))
        tokens = (context.set_app(app), context.set_session_id(root.owner.session_id))
        invocation = TASK_CALL.set(TaskCall(invocation_id=f"inv-{tool}-{len(arguments)}"))
        try:
            return await external.call(
                "viewer", client, tool, arguments, auth_scope="trusted-test-scope"
            )
        finally:
            TASK_CALL.reset(invocation)
            for token in reversed(tokens):
                context.reset(token)


def _tasks(history: Any, sid: str) -> list[Any]:
    tasks = [history.task_snapshot(handle) for handle in history.task_handles(session_id=sid)]
    return [task for task in tasks if task.kind == "MCP"]


@pytest.mark.asyncio
@pytest.mark.timeout(60)  # An in-memory MCP session opens and closes.
async def test_a_view_only_call_records_a_size_and_hash_not_its_content(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """SABOTAGE: drop ``stored_reply`` in ``ExternalTasks._call`` and the chunk is stored."""
    app, root = await _environment(arc, tmp_path)
    history = app.state.task_history
    external = ExternalTasks(app)
    client = Client(_server(threading.Event(), threading.Event()))
    image = "P" * 10_000
    try:
        hidden = await _view_call(
            app, root, external, client, "read_bytes", {"offset": 0, "page_image": image}, VIEW_ONLY
        )
        seen = await _view_call(
            app, root, external, client, "read_bytes", {"offset": 1}, MODEL_SEES
        )
    finally:
        await external.close()
        await app.state.task_recovery.drain()

    # The view gets the whole reply either way.
    assert hidden.structured_content["bytes"] == CHUNK
    assert seen.structured_content["bytes"] == CHUNK

    def recorded() -> list[tuple[Any, Any, Any]]:
        return [
            (task, history.read_content(task.submission), history.read_content(task.result))
            for task in _tasks(history, root.owner.session_id)
        ]

    records = await asyncio.to_thread(recorded)
    (task, submission, result), (_task, plain_submission, plain_result) = sorted(
        records, key=lambda row: "view_only_arguments" not in row[1]["arguments"]
    )
    assert task.status == "completed" and task.settlement == "external_acknowledged"
    assert submission["attribution"]["origin"] == VIEW_ORIGIN
    kept = submission["arguments"]["view_only_arguments"]
    assert kept["reason"] == view_only_results.VIEW_ONLY_CONTENT_NOT_STORED
    assert kept["keys"] == ["offset", "page_image"] and kept["bytes"] > 10_000
    record = result["structuredContent"]["view_only_result"]
    wire = {
        "content": [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            for block in hidden.content
        ],
        "structuredContent": hidden.structured_content,
        "isError": False,
    }
    body = json.dumps(wire, sort_keys=True, separators=(",", ":"), default=str).encode()
    assert record == {
        "reason": view_only_results.VIEW_ONLY_CONTENT_NOT_STORED,
        "tool": "read_bytes",
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "outcome": "ok",
    }
    assert CHUNK not in json.dumps(result) and image not in json.dumps(submission)
    # A tool the model can see keeps its normal record: arguments and reply as sent.
    assert plain_submission["arguments"] == {"offset": 1}
    assert plain_result["structuredContent"]["bytes"] == CHUNK
    assert plain_submission["attribution"]["origin"] == VIEW_ORIGIN


@pytest.mark.asyncio
@pytest.mark.timeout(60)  # An in-memory MCP session opens and closes.
async def test_a_view_poll_cut_off_ends_unknown_and_wakes_no_one(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """SABOTAGE: record ``self._attention`` in ``abandon_call`` and an episode opens."""
    app, root = await _environment(arc, tmp_path)
    history = app.state.task_history
    external = ExternalTasks(app)
    started, release = threading.Event(), threading.Event()
    client = Client(_server(started, release))
    cancel = threading.Event()
    sid = root.owner.session_id
    mail_before = await asyncio.to_thread(history.pending_mail, sid)
    try:
        call = asyncio.ensure_future(
            _view_call(app, root, external, client, "poll", {}, VIEW_ONLY, cancel=cancel)
        )
        await asyncio.to_thread(started.wait, 20)
        cancel.set()  # the view's page went away
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        release.set()
        await external.close()
        await app.state.task_recovery.drain()

    (task,) = await asyncio.to_thread(_tasks, history, sid)
    assert task.status == "cancelled" and task.settlement == "unknown"
    assert task.raw_status == view_call_cancel.VIEW_CALL_CANCELLED
    assert task.terminal_at is not None and task.attention_episode is None
    assert task.progress.message == view_call_cancel.VIEW_CALL_CANCELLED_TEXT
    assert await asyncio.to_thread(history.pending_mail, sid) == mail_before


@pytest.mark.asyncio
@pytest.mark.timeout(60)  # An in-memory MCP session opens and closes.
async def test_a_large_model_visible_reply_is_recorded_as_its_head_and_a_file(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GG-F3: any oversized reply, not only a view's, stays out of the ledger's segment."""
    monkeypatch.setenv("CLIO_TOOL_RESULT_RECORD_BYTES", "65536")
    monkeypatch.setattr(
        "clio_agent.tools.servers.shell_spill_store.spill_directory",
        lambda _root, session_id=None: tmp_path / "tool-output",
    )
    app, root = await _environment(arc, tmp_path)
    history = app.state.task_history
    external = ExternalTasks(app)
    client = Client(_server(threading.Event(), threading.Event()))
    try:
        reply = await _view_call(
            app, root, external, client, "read_bytes", {"offset": 2}, MODEL_SEES
        )
    finally:
        await external.close()
        await app.state.task_recovery.drain()
    assert reply.structured_content["bytes"] == CHUNK  # the caller's copy is whole

    def recorded() -> Any:
        (task,) = _tasks(history, root.owner.session_id)
        return history.read_content(task.result)

    result = await asyncio.to_thread(recorded)
    stored = result["structuredContent"]["stored_result"]
    assert stored["reason"] == "tool_result_stored_in_file"
    assert CHUNK in Path(stored["file"]).read_text()
    assert len(json.dumps(result)) < 20_000
