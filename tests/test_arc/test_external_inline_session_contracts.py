"""Legacy callback attribution and exclusive stdio retirement against actual SDK/Core."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionReply, TaskSnapshot
from clio_agent.gact import context
from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_conversation_owner_contracts import create_environment

SERVER = """
import json
import os
import sys
from pathlib import Path
import psutil
from fastmcp import Context, FastMCP

Path(sys.argv[1]).write_text(json.dumps({"pid": os.getpid(), "created_at": psutil.Process().create_time()}))
server = FastMCP("legacy-inline-input")

@server.tool
async def choose(tag: str, ctx: Context) -> dict[str, str]:
    response = await ctx.elicit("Choose a value for " + tag, response_type=str)
    if response.action != "accept":
        return {"tag": tag, "value": response.action}
    return {"tag": tag, "value": response.data}

server.run(transport="stdio", show_banner=False)
"""


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real subprocess transports must close and their processes retire.
@pytest.mark.parametrize("cancel_before_reply", [False, True])
async def test_concurrent_legacy_callbacks_have_bound_invocations_and_retire_processes(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_before_reply: bool
) -> None:
    """Two actual stdio callbacks lose caller ContextVars without losing owner attribution."""
    app, conversations, root = await create_environment(arc, tmp_path)
    history = conversations.history
    app.state.task_recovery = conversations.recovery
    app.state.questions = QuestionCommands(history)

    async def cancel(task: TaskSnapshot, caller: Any) -> dict[str, Any]:
        if task.kind == "Question":
            closed = await asyncio.to_thread(app.state.questions.cancel, task.handle)
            return {"handle": closed.handle, "status": closed.status}
        return await conversations.cancel(caller, task.handle)

    app.state.task_operations = TaskOperations(history, cancel)
    monkeypatch.setattr("clio_agent.gact.agent_elicitation._enabled", lambda: False)

    def setup_workspace() -> None:
        app.state.workspaces = WorkspaceStore(path=tmp_path / "workspaces.json")
        workspace = app.state.workspaces.create(name="MCP input", root_path=str(tmp_path))
        # This fixture's existing root predates its workspace setup.
        session = app.state.sessions.get(root.owner.session_id)
        assert session is not None
        session.workspace_id = workspace.id
        app.state.sessions._persist(session)

    await asyncio.to_thread(setup_workspace)
    external = ExternalTasks(app)
    recording_release = asyncio.Event()
    original_defer = external.recovery.defer_recording

    def defer_cancel_recording(identity: str, operation: Callable[[], object]) -> None:
        if not cancel_before_reply or not identity.startswith("external-cancel:"):
            original_defer(identity, operation)
            return

        async def record() -> bool:
            if not recording_release.is_set():
                return False
            await history.io.run(operation)
            return True

        external.recovery.defer(identity, record)

    monkeypatch.setattr(external.recovery, "defer_recording", defer_cancel_recording)
    script = tmp_path / "legacy_input_server.py"
    script.write_text(SERVER, encoding="utf-8")
    receipts = [tmp_path / f"stdio-{tag}.json" for tag in ("first", "second")]
    (tmp_path / "owned-processes.json").write_text(
        json.dumps(
            {
                "purpose": "Legacy input contract",
                "command": sys.executable,
                "script": str(script),
                "receipts": [str(path) for path in receipts],
                "ports": [],
            }
        ),
        encoding="utf-8",
    )
    questions: asyncio.Queue[TaskSnapshot] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    app.state.questions.on_change = lambda task: (
        loop.call_soon_threadsafe(questions.put_nowait, task)
        if task.raw_status == "pending"
        else None
    )
    callbacks: list[tuple[str, str, Any]] = []
    original_input = external._input

    async def observed_input(host: ExternalConnection, params: Any, request: Any) -> Any:
        assert host.inline.get() is None, "Legacy stdio did not propagate the submitting context"
        assert host.invocation is not None and host.isolated
        callbacks.append((host.backend.session, host.invocation.handle, request.request_id))
        return await original_input(host, params, request)

    monkeypatch.setattr(external, "_input", observed_input)

    async def call(tag: str, receipt: Path) -> Any:
        app_token, session_token = (
            context.set_app(app),
            context.set_session_id(root.owner.session_id),
        )
        invocation_token = TASK_CALL.set(TaskCall(invocation_id="legacy-" + tag))
        try:
            return await external.call(
                "legacy-input",
                Client(
                    StdioTransport(
                        sys.executable, ["-B", str(script), str(receipt)], cwd=str(tmp_path)
                    ),
                    mode="legacy",
                    timeout=20,
                ),
                "choose",
                {"tag": tag},
                auth_scope="trusted-test-scope",
            )
        finally:
            TASK_CALL.reset(invocation_token)
            context.reset(session_token)
            context.reset(app_token)

    calls = [
        asyncio.create_task(call(tag, receipt))
        for tag, receipt in zip(("first", "second"), receipts, strict=True)
    ]
    posted: list[TaskSnapshot] = []
    try:
        # Posting and routing annotation both publish pending snapshots. Wait
        # for two Question identities, not two revisions of the first Question.
        by_handle: dict[str, TaskSnapshot] = {}
        deadline = loop.time() + 30
        while len(by_handle) < 2:
            question = await asyncio.wait_for(questions.get(), max(0.0, deadline - loop.time()))
            previous = by_handle.get(question.handle)
            if previous is not None:
                assert question.owner == previous.owner
                assert question.invocation_id == previous.invocation_id
            by_handle[question.handle] = question
        posted = list(by_handle.values())
        assert len({task.owner.parent_handle for task in posted}) == 2
        assert all(not call.done() for call in calls)
        assert len(callbacks) == 2 and len({item[0] for item in callbacks}) == 2
        assert {item[1] for item in callbacks} == {task.owner.parent_handle for task in posted}
        for question in reversed(posted):
            assert question.owner.parent_handle is not None
            invocation = await asyncio.to_thread(
                history.task_snapshot, question.owner.parent_handle
            )
            assert invocation is not None
            if cancel_before_reply:
                acknowledgement = await external.cancel(invocation.handle)
                assert acknowledgement["cancellation_requested"] is True
                assert acknowledgement["settlement"] == "unknown"
                assert acknowledgement["recording"] == "unconfirmed"
                assert external.recovery.is_pending("external-cancel:" + invocation.handle)
            value = "answer-" + invocation.invocation_id.removeprefix("legacy-")
            await asyncio.to_thread(
                app.state.questions.answer,
                question.handle,
                QuestionReply(metadata={"value": value}, responder_human="human"),
            )
        results = await asyncio.gather(*calls)
        assert [result.structured_content for result in results] == [
            {"tag": "first", "value": "answer-first"},
            {"tag": "second", "value": "answer-second"},
        ]
        for question in posted:
            assert question.owner.parent_handle is not None
            invocation = await asyncio.to_thread(
                history.task_snapshot, question.owner.parent_handle
            )
            assert invocation is not None and invocation.raw_status == "inline"
            assert invocation.backend_observation is not None
            assert invocation.status == "completed" and invocation.result is not None
            assert invocation.terminal_at is not None
            assert invocation.settlement == "external_acknowledged"
            retained = await asyncio.to_thread(history.read_content, invocation.result)
            tag = invocation.invocation_id.removeprefix("legacy-")
            assert retained["structuredContent"] == {"tag": tag, "value": "answer-" + tag}
            answered = await asyncio.to_thread(history.task_snapshot, question.handle)
            assert answered is not None and answered.status == "completed"
        if external._retiring:
            await asyncio.gather(*tuple(external._retiring))
        assert not external.connections, "Completed exclusive sessions must not accumulate"
    finally:
        # Release every owned input before awaiting transport shutdown, even on failure.
        recording_release.set()
        pending = await asyncio.to_thread(history.task_handles, kind="Question", active_only=True)
        for handle in pending:
            await asyncio.to_thread(app.state.questions.cancel, handle)
        await asyncio.gather(*calls, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
    if cancel_before_reply:
        for question in posted:
            assert question.owner.parent_handle is not None
            invocation = await asyncio.to_thread(
                history.task_snapshot, question.owner.parent_handle
            )
            assert invocation is not None
            assert invocation.cancellation_requested_at is not None
    for receipt in receipts:
        process = json.loads(receipt.read_text())
        try:
            actual = psutil.Process(process["pid"])
        except psutil.NoSuchProcess:
            continue
        assert actual.create_time() != process["created_at"], (
            "Owned stdio process survived retirement"
        )
