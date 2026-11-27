"""One prompt per server ask across tasks, and withdrawal when a task stops asking."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp_types
import pytest
from fastmcp_tasks.client_models import (
    ClientGetTaskResult,
    UpdateTaskRequest,
    UpdateTaskRequestParams,
)

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionReply
from clio_agent.gact import external_task_inputs
from clio_agent.gact.native_work import now
from clio_agent.gact.session_records import PageQuery
from clio_agent.tools.task_transport import ObservedTaskSession
from tests.test_arc.test_external_input_identity_contracts import (
    accepted,
    environment,
    question_ready,
)


def _questions(app: Any) -> list[str]:
    history = app.state.task_history
    root = history.latest_assignment(app.state.sessions.select(PageQuery(archived=None))[0].id)
    return list(history.task_handles(kind="Question", human_id=root.owner.human_id))


async def _close(external: Any, conversations: Any, workers: list[asyncio.Task[Any]]) -> None:
    for worker in workers:
        if not worker.done():
            worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    await external.close()
    await conversations.shutdown()
    await conversations.recovery.drain()


@pytest.mark.asyncio
async def test_two_tasks_parked_on_one_ask_show_one_prompt(arc: ARCMemory, tmp_path: Path) -> None:
    """Relay parks concurrent installs on one ask_id: the person answers it once."""
    app, conversations, external, host, params = await environment(arc, tmp_path)
    workers: list[asyncio.Task[Any]] = []
    try:
        lead = await accepted(app, external, host, params, task_id="job-a", keys=("ask-1",))
        follow = await accepted(app, external, host, params, task_id="job-b", keys=("ask-1",))
        workers.append(
            asyncio.create_task(
                external._input(
                    host,
                    params,
                    SimpleNamespace(session=host.client.session, request_id="task-job-a-ask-1"),
                )
            )
        )
        shown = await question_ready(app, lead, "ask-1")
        workers.append(
            asyncio.create_task(
                external._input(
                    host,
                    params,
                    SimpleNamespace(session=host.client.session, request_id="task-job-b-ask-1"),
                )
            )
        )
        async with asyncio.timeout(5):
            while len(external.question_waiters.get(shown, ())) < 2:
                await asyncio.sleep(0.01)
        assert await asyncio.to_thread(_questions, app) == [shown]
        assert (
            app.state.task_history.task_snapshot(external_task_inputs.question_id(follow, "ask-1"))
            is None
        )

        await asyncio.to_thread(
            app.state.questions.answer, shown, QuestionReply(metadata={"format": "png"})
        )
        first, second = await asyncio.wait_for(asyncio.gather(*workers), 5)
        assert first.content == second.content == {"format": "png"}
        for handle in (lead, follow):
            details = await asyncio.to_thread(
                lambda h=handle: external._details(app.state.task_history.task_snapshot(h))
            )
            assert details["answers"]["ask-1"]["content"] == {"format": "png"}
        assert await asyncio.to_thread(_questions, app) == [shown]
    finally:
        await _close(external, conversations, workers)


@pytest.mark.asyncio
async def test_a_task_that_stops_asking_withdraws_its_prompt(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP has no task-status notification: tasks/get polling sees the task move on."""
    monkeypatch.setattr(external_task_inputs, "INPUT_POLL_S", 0.05)
    app, conversations, external, host, params = await environment(arc, tmp_path)
    workers: list[asyncio.Task[Any]] = []
    try:
        handle = await accepted(app, external, host, params, task_id="job-a", keys=("ask-1",))
        status = {"value": "input_required"}

        async def poll() -> ClientGetTaskResult:
            stamp = now()
            return ClientGetTaskResult.model_validate(
                {
                    "resultType": "complete",
                    "taskId": "job-a",
                    "status": status["value"],
                    "createdAt": stamp,
                    "lastUpdatedAt": stamp,
                    **(
                        {
                            "inputRequests": {
                                "ask-1": {
                                    "method": "elicitation/create",
                                    "params": params.model_dump(
                                        mode="json", by_alias=True, exclude_none=True
                                    ),
                                }
                            }
                        }
                        if status["value"] == "input_required"
                        else {}
                    ),
                }
            )

        monkeypatch.setattr(host.calls["job-a"].task, "status", poll)
        workers.append(
            asyncio.create_task(
                external._input(
                    host,
                    params,
                    SimpleNamespace(session=host.client.session, request_id="task-job-a-ask-1"),
                )
            )
        )
        shown = await question_ready(app, handle, "ask-1")
        await asyncio.sleep(0.2)
        assert not workers[0].done()  # still asking: the prompt stays

        status["value"] = "working"  # another task's answer moved this one on
        answer = await asyncio.wait_for(workers[0], 5)
        assert answer.action == "cancel"
        assert app.state.task_history.task_snapshot(shown).terminal_at is not None
        details = await asyncio.to_thread(
            lambda h=handle: external._details(app.state.task_history.task_snapshot(h))
        )
        assert details["withdrawing"] == {"ask-1": True}
        assert details["withdrawn"] == {"ask-1": 1}
        assert "ask-1" not in details.get("answers", {})
        # Asking again later is a fresh prompt, not the withdrawn one.
        assert external_task_inputs.question_id(handle, "ask-1", 1) != shown
    finally:
        await _close(external, conversations, workers)


@pytest.mark.asyncio
async def test_a_shared_prompt_answered_elsewhere_withdraws_for_every_task(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ask is answered outside CLIO: both parked tasks move on, the one prompt closes,
    and neither task opens a prompt of its own."""
    monkeypatch.setattr(external_task_inputs, "INPUT_POLL_S", 0.05)
    app, conversations, external, host, params = await environment(arc, tmp_path)
    workers: list[asyncio.Task[Any]] = []
    try:
        lead = await accepted(app, external, host, params, task_id="job-a", keys=("ask-1",))
        follow = await accepted(app, external, host, params, task_id="job-b", keys=("ask-1",))
        status = {"value": "input_required"}

        def poller(task_id: str) -> Any:
            async def poll() -> ClientGetTaskResult:
                stamp = now()
                asking = status["value"] == "input_required"
                return ClientGetTaskResult.model_validate(
                    {
                        "resultType": "complete",
                        "taskId": task_id,
                        "status": status["value"],
                        "createdAt": stamp,
                        "lastUpdatedAt": stamp,
                        **(
                            {
                                "inputRequests": {
                                    "ask-1": {
                                        "method": "elicitation/create",
                                        "params": params.model_dump(
                                            mode="json", by_alias=True, exclude_none=True
                                        ),
                                    }
                                }
                            }
                            if asking
                            else {}
                        ),
                    }
                )

            return poll

        for task_id in ("job-a", "job-b"):
            monkeypatch.setattr(host.calls[task_id].task, "status", poller(task_id))
        for task_id in ("job-a", "job-b"):
            workers.append(
                asyncio.create_task(
                    external._input(
                        host,
                        params,
                        SimpleNamespace(
                            session=host.client.session, request_id=f"task-{task_id}-ask-1"
                        ),
                    )
                )
            )
            if task_id == "job-a":
                shown = await question_ready(app, lead, "ask-1")
        async with asyncio.timeout(5):
            while len(external.question_waiters.get(shown, ())) < 2:
                await asyncio.sleep(0.01)

        status["value"] = "working"
        first, second = await asyncio.wait_for(asyncio.gather(*workers), 5)
        assert first.action == second.action == "cancel"
        assert app.state.task_history.task_snapshot(shown).terminal_at is not None
        assert await asyncio.to_thread(_questions, app) == [shown]
        for handle in (lead, follow):
            details = await asyncio.to_thread(
                lambda h=handle: external._details(app.state.task_history.task_snapshot(h))
            )
            assert "ask-1" not in details.get("answers", {})
    finally:
        await _close(external, conversations, workers)


@pytest.mark.asyncio
async def test_editing_task_details_never_changes_the_stored_record(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """Take 1r: a nested edit reached the cached record, failing its integrity check."""
    app, conversations, external, _host, _params = await environment(arc, tmp_path)
    try:
        history = app.state.task_history
        sid = app.state.sessions.select(PageQuery(archived=None))[0].id
        value = {"requests": {"ask-1": {"method": "x"}}, "withdrawn": {"ask-1": 1}}
        reference = await asyncio.to_thread(history.write_content, sid, value)
        details = await asyncio.to_thread(external._details, SimpleNamespace(details=reference))
        details["withdrawn"]["ask-1"] = 2
        details["requests"]["ask-1"]["method"] = "y"
        assert await asyncio.to_thread(history.read_content, reference) == value
        assert await asyncio.to_thread(history.write_content, sid, value) == reference
    finally:
        await _close(external, conversations, [])


@pytest.mark.asyncio
async def test_an_update_with_every_input_withdrawn_is_not_sent() -> None:
    sent: list[Any] = []

    async def guard(_session: Any, _task: str, answers: dict[str, Any]) -> dict[str, Any]:
        sent.append(answers)
        return {}

    session = SimpleNamespace(_guard_task_update=guard)
    request = UpdateTaskRequest(
        params=UpdateTaskRequestParams(task_id="job-a", input_responses={"ask-1": {"action": "x"}})
    )
    result = await ObservedTaskSession.send_request(session, request, mcp_types.Result)  # type: ignore[arg-type]
    assert isinstance(result, mcp_types.Result)
    assert sent == [{"ask-1": {"action": "x"}}]
