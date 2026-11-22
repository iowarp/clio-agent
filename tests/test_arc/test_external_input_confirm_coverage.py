"""A person's approval of an MCP call answers that call's own plain confirm, visibly."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import mcp_types
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionReply
from clio_agent.gact import external_task_inputs
from clio_agent.gact import mcp_confirm_coverage as coverage
from tests.test_arc.test_external_input_identity_contracts import (
    accepted,
    environment,
    question_ready,
)

APPROVE = {
    "type": "object",
    "properties": {"approve": {"type": "boolean", "title": "Approve", "default": False}},
    "required": ["approve"],
}


def _confirm(schema: dict[str, Any]) -> Any:
    return mcp_types.ElicitRequestFormParams(
        message="Submit the resource_graph job to Ares?",
        requested_schema=schema,
        _meta=cast(mcp_types.RequestParamsMeta, {"x-clio-agent/audience": "human"}),
    )


def _permission(app: Any, *, grantor: str | None, status: str = "resolved") -> None:
    sid = app.state.task_history.task_snapshot(
        next(iter(app.state.task_history.task_handles(kind="MCP")))
    ).owner.session_id
    row: dict[str, Any] = {
        "id": "perm_submit",
        "session_id": sid,
        "tool_call": {"tool_name": "relay_run", "input": {}, "call_id": "invoke:job-a"},
        "status": status,
        "action": "allow",
    }
    if grantor:
        row["grantor"] = grantor
    app.state.permissions = {"perm_submit": row}


def _request(host: Any) -> Any:
    return SimpleNamespace(session=host.client.session, request_id="task-job-a-ask-1")


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        ({"type": "object", "properties": {}}, {}),
        ({"type": "object"}, {}),
        (APPROVE, {"approve": True}),
    ],
)
def test_a_plain_confirm_offers_nothing_to_choose(schema: dict[str, Any], content: Any) -> None:
    assert coverage.plain_confirm(_confirm(schema)) == content


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"site": {"type": "string"}}},
        {"type": "object", "properties": {"nodes": {"type": "integer"}}},
        {"type": "object", "properties": {"mpi": {"type": "string", "enum": ["a", "b"]}}},
        {"type": "object", "properties": {"a": {"type": "boolean"}, "b": {"type": "boolean"}}},
        {"type": "object", "properties": {"ok": {"type": "boolean", "const": True}}},
    ],
)
def test_fields_choices_or_numbers_are_not_a_plain_confirm(schema: dict[str, Any]) -> None:
    assert coverage.plain_confirm(_confirm(schema)) is None


@pytest.mark.asyncio
async def test_a_person_allowed_call_answers_its_plain_confirm_under_the_call(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, conversations, external, host, _ = await environment(arc, tmp_path)
    params = _confirm(APPROVE)
    try:
        handle = await accepted(app, external, host, params, task_id="job-a", keys=("ask-1",))
        _permission(app, grantor="user")
        answer = await asyncio.wait_for(external._input(host, params, _request(host)), 5)
        assert answer.action == "accept" and answer.content == {"approve": True}

        qid = external_task_inputs.question_id(handle, "ask-1")
        question = app.state.task_history.task_snapshot(qid)
        # The Question stays under the call, answered, with the server's own message.
        assert question.raw_status == "answered"
        assert question.invocation_id == "invoke:job-a"
        assert question.description == params.message
        raw = await asyncio.to_thread(app.state.task_history.read_content, question.result)
        reply = QuestionReply.model_validate(raw)
        assert reply.selected_options == ("yes",)
        assert reply.answer == coverage.COVERED_ANSWER
        resolution = await asyncio.to_thread(app.state.questions.resolution, qid)
        assert resolution["reason"] == coverage.REASON_COVERED
        assert resolution["permission_id"] == "perm_submit"
        snapshot = app.state.task_history.task_snapshot(handle)
        details = await asyncio.to_thread(external._details, snapshot)
        assert details["covered_inputs"]["ask-1"]["reason"] == coverage.REASON_COVERED
    finally:
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "grantor", "status"),
    [
        (APPROVE, None, "auto_approved"),  # a rule or mode allowed it: no person answered
        (APPROVE, "reviewer", "resolved"),
        ({"type": "object", "properties": {"nodes": {"type": "integer"}}}, "user", "resolved"),
    ],
)
async def test_policy_allowed_calls_and_real_forms_still_ask(
    arc: ARCMemory, tmp_path: Path, schema: dict[str, Any], grantor: str | None, status: str
) -> None:
    app, conversations, external, host, _ = await environment(arc, tmp_path)
    params = _confirm(schema)
    worker: asyncio.Task[Any] | None = None
    try:
        handle = await accepted(app, external, host, params, task_id="job-a", keys=("ask-1",))
        _permission(app, grantor=grantor, status=status)
        worker = asyncio.create_task(external._input(host, params, _request(host)))
        qid = await question_ready(app, handle, "ask-1")
        await asyncio.sleep(0.1)
        assert not worker.done()
        assert app.state.task_history.task_snapshot(qid).raw_status == "pending"
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


JOB = {"io.iowarp.clio-relay/job": {"phase": "planned", "nodes": 2, "script": "srun ./a.out"}}
CARD = {
    "type": "object",
    "properties": {
        "approve": {
            "type": "string",
            "oneOf": [
                {"const": "approve", "title": "Approve"},
                {"const": "decline", "title": "Decline"},
            ],
        }
    },
    "required": ["approve"],
}


def test_namespaced_meta_keeps_typed_keys_and_params_win() -> None:
    from clio_agent.gact.elicitation_schema import namespaced_meta

    assert namespaced_meta({"progressToken": 3}) is None
    carried = {"io.iowarp.clio-relay/job": {"phase": "planned"}, "progressToken": 1}
    own = {"io.iowarp.clio-relay/job": {"phase": "own"}}
    assert namespaced_meta(None, carried) == {"io.iowarp.clio-relay/job": {"phase": "planned"}}
    assert namespaced_meta(own, carried) == own


def test_a_request_carrying_a_typed_plan_is_never_plain() -> None:
    assert coverage.plain_confirm(_confirm(APPROVE), JOB) is None
    assert coverage.plain_confirm(_confirm(CARD)) is None
    # MCP's own correlation and CLIO's routing hints carry nothing for the person.
    related = {"io.modelcontextprotocol/related-task": {"taskId": "t"}}
    assert coverage.plain_confirm(_confirm(APPROVE), related) == {"approve": True}


@pytest.mark.asyncio
async def test_a_task_replys_meta_reaches_the_task_and_its_card_and_still_asks(
    arc: ARCMemory, tmp_path: Path
) -> None:
    from clio_agent.gact.question_surface import question_row
    from clio_agent.gact.routes.interactions import _question_interaction
    from clio_agent.gact.routes.tasks import process_view

    app, conversations, external, host, _ = await environment(arc, tmp_path)
    # A 2026-07-28 embedded request has no _meta: the plan rides on the task reply.
    params = mcp_types.ElicitRequestFormParams(message="Submit to Ares?", requested_schema=APPROVE)
    worker: asyncio.Task[Any] | None = None
    try:
        handle = await accepted(
            app, external, host, params, task_id="job-a", keys=("ask-1",), meta=JOB
        )
        task = app.state.task_history.task_snapshot(handle)
        assert task.metadata == JOB
        assert process_view(app, task.model_dump(mode="json"))["metadata"] == JOB
        _permission(app, grantor="user")  # a plain confirm would be answered by this
        worker = asyncio.create_task(external._input(host, params, _request(host)))
        qid = await question_ready(app, handle, "ask-1")
        await asyncio.sleep(0.1)
        assert not worker.done()  # the card is the one ask
        row = await asyncio.to_thread(question_row, app, qid)
        interaction = _question_interaction(app, row)
        assert interaction.payload["metadata"]["elicitation"]["meta"] == JOB
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
