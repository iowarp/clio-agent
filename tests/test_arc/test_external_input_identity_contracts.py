"""External task input identity, partial answers and recoverable callback replies."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import BackendReference, QuestionReply, TaskSnapshot, argument_digest
from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
from clio_agent.gact.native_work import now
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.tools.task_client import TaskAwareClient, TaskSubmission, restore_native_call
from tests.test_arc.test_conversation_owner_contracts import create_environment


async def environment(
    arc: ARCMemory, tmp_path: Path
) -> tuple[Any, Any, ExternalTasks, ExternalConnection, Any]:
    """Use actual Core and Question owners; substitute only the test transport session."""
    app, conversations, root = await create_environment(arc, tmp_path)
    app.state.questions = QuestionCommands(conversations.history)

    async def cancel(task: TaskSnapshot, caller: Any) -> dict[str, Any]:
        pytest.fail("Input callback/waiter cancellation must not cancel accepted server work")

    app.state.task_operations = TaskOperations(conversations.history, cancel)
    app.state.task_recovery = conversations.recovery
    external = ExternalTasks(app)
    backend = BackendReference(
        owner="external_mcp",
        server="verified-server",
        auth_scope="human-scope",
        session="verified-native-session",
        transport="StreamableHTTPTransport",
    )
    session = object()
    host = ExternalConnection(backend)
    host.client = cast(TaskAwareClient, SimpleNamespace(session=session))
    params = mcp_types.ElicitRequestFormParams(
        message="Which format?",
        requested_schema={
            "type": "object",
            "properties": {"format": {"type": "string"}},
            "required": ["format"],
        },
        _meta=cast(mcp_types.RequestParamsMeta, {"x-clio-agent/audience": "human"}),
    )
    return app, conversations, external, host, params


async def accepted(
    app: Any,
    external: ExternalTasks,
    host: ExternalConnection,
    params: Any,
    *,
    task_id: str = "job-a-b",
    keys: tuple[str, ...] = ("c-d",),
) -> str:
    """Record a native acceptance and its actual SDK status reply without executing work."""

    def record() -> str:
        history = app.state.task_history
        root = history.latest_assignment(app.state.sessions.select(PageQuery(archived=None))[0].id)
        handle, stamp = "task_" + hashlib.sha256(task_id.encode()).hexdigest(), now()
        submission = TaskSubmission(
            handle=handle,
            tool_name="convert",
            tool_version="captured-v1",
            context=history.write_content(root.owner.session_id, {"arguments": {}}),
            argument_digest=argument_digest({}),
            output_schema=None,
        )
        current = TaskSnapshot(
            handle=handle,
            kind="MCP",
            label="Convert data",
            description="Convert data",
            description_missing=False,
            owner=root.owner.model_copy(update={"parent_handle": root.handle}),
            tool="convert",
            invocation_id="invoke:" + task_id,
            created_at=stamp,
            updated_at=stamp,
            backend=host.backend.model_copy(update={"task_id": task_id}),
            submission=submission.context,
            status="running",
            raw_status="working",
        )
        history.record_snapshot(current, observation_id="accept:" + task_id)
        sdk = Client(FastMCP("unused-input-transport"))
        call = restore_native_call(
            sdk, submission, task_id=task_id, status="working", created_at=stamp, updated_at=stamp
        )
        host.calls[task_id] = call
        reply = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "input_required",
                "createdAt": stamp,
                "lastUpdatedAt": stamp,
                "inputRequests": {
                    key: {
                        "method": "elicitation/create",
                        "params": params.model_dump(mode="json", by_alias=True, exclude_none=True),
                    }
                    for key in keys
                },
            }
        )
        external._record_reply(host, call, reply)
        return handle

    return await asyncio.to_thread(record)


async def question_ready(app: Any, handle: str, key: str) -> str:
    """Bound only the test controller while the real input owner posts its Question."""
    identity = "task_" + hashlib.sha256((handle + "\0" + key).encode()).hexdigest()
    async with asyncio.timeout(5):
        while (
            task := await asyncio.to_thread(app.state.task_history.task_snapshot, identity)
        ) is None or task.raw_status != "pending":
            await asyncio.sleep(0.001)
    return identity


@pytest.mark.asyncio
async def test_hyphenated_input_reuses_confirmed_answer_after_callback_reply_loss(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, conversations, external, host, params = await environment(arc, tmp_path)
    worker: asyncio.Task[Any] | None = None
    try:
        handle = await accepted(app, external, host, params)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        worker = asyncio.create_task(external._input(host, params, request))
        qid = await question_ready(app, handle, "c-d")
        await asyncio.to_thread(
            app.state.questions.answer, qid, QuestionReply(metadata={"format": "png"})
        )
        answer = await asyncio.wait_for(worker, 5)
        assert answer.action == "accept" and answer.content == {"format": "png"}
        current = await asyncio.to_thread(app.state.task_history.task_snapshot, handle)
        details = await asyncio.to_thread(external._details, current)
        assert details["answers"]["c-d"]["content"] == {"format": "png"}
        questions = app.state.task_history.task_handles(kind="Question")
        # Discard the first callback response as if transport delivery was lost.
        replay = await external._input(host, params, request)
        assert replay == answer
        assert app.state.task_history.task_handles(kind="Question") == questions
        assert app.state.task_history.task_snapshot(handle).terminal_at is None
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "payload", "ambiguous-id"])
async def test_input_identity_rejection_has_no_question_or_backend_effects(
    arc: ARCMemory, tmp_path: Path, mismatch: str
) -> None:
    app, conversations, external, host, params = await environment(arc, tmp_path)
    try:
        await accepted(app, external, host, params)
        request = SimpleNamespace(session=host.client.session, request_id="task-job-a-b-c-d")
        if mismatch == "session":
            request.session = object()
        elif mismatch == "payload":
            params = params.model_copy(update={"message": "Changed server content"})
        else:
            await accepted(app, external, host, params, task_id="job-a", keys=("b-c-d",))
        revision = app.state.task_history.revision
        with pytest.raises(ValueError, match="session|identity/payload"):
            await external._input(host, params, request)
        assert app.state.task_history.revision == revision
        assert not app.state.task_history.task_handles(kind="Question")
    finally:
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_reused_server_key_cannot_replace_original_input_content(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, conversations, external, host, params = await environment(arc, tmp_path)
    try:
        handle = await accepted(app, external, host, params)
        original = app.state.task_history.task_snapshot(handle)
        reply = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": "job-a-b",
                "status": "input_required",
                "createdAt": original.created_at,
                "lastUpdatedAt": now(),
                "inputRequests": {
                    "c-d": {"method": "elicitation/create", "params": {"message": "Changed"}}
                },
            }
        )
        with pytest.raises(ValueError, match="reused an input key"):
            await asyncio.to_thread(external._record_reply, host, host.calls["job-a-b"], reply)
        assert app.state.task_history.task_snapshot(handle) == original
    finally:
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_partial_answers_survive_waiter_cancellation_and_transport_reentry(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, conversations, external, host, params = await environment(arc, tmp_path)
    workers: list[asyncio.Task[Any]] = []
    try:
        handle = await accepted(app, external, host, params, keys=("first", "second"))
        for key in ("first", "second"):
            workers.append(
                asyncio.create_task(
                    external._input(
                        host,
                        params,
                        SimpleNamespace(
                            session=host.client.session, request_id="task-job-a-b-" + key
                        ),
                    )
                )
            )
        first, second = await asyncio.gather(
            question_ready(app, handle, "first"), question_ready(app, handle, "second")
        )
        await asyncio.to_thread(
            app.state.questions.answer, first, QuestionReply(metadata={"format": "png"})
        )
        await asyncio.wait_for(workers[0], 5)
        workers[1].cancel()
        await asyncio.gather(workers[1], return_exceptions=True)
        retained = app.state.task_history.task_snapshot(handle)
        details = await asyncio.to_thread(external._details, retained)
        assert set(details["answers"]) == {"first"}
        assert app.state.task_history.task_snapshot(second).raw_status == "pending"
        assert retained.terminal_at is None
        host.client = cast(TaskAwareClient, SimpleNamespace(session=object()))
        pending = asyncio.create_task(
            external._input(
                host,
                params,
                SimpleNamespace(session=host.client.session, request_id="task-job-a-b-second"),
            )
        )
        workers.append(pending)
        await asyncio.to_thread(
            app.state.questions.answer, second, QuestionReply(metadata={"format": "webp"})
        )
        answer = await asyncio.wait_for(pending, 5)
        assert answer.content == {"format": "webp"}
        final = await asyncio.to_thread(
            external._details, app.state.task_history.task_snapshot(handle)
        )
        assert final["answers"]["first"]["content"] == {"format": "png"}
        assert final["answers"]["second"]["content"] == {"format": "webp"}
        history = app.state.task_history
        human = (await asyncio.to_thread(history.task_snapshot, handle)).owner.human_id
        assert len(history.task_handles(kind="Question", human_id=human)) == 2
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
