"""Unit tests for the transport-abstracted expert invoker (epic #667, #671).

The hinge: in-process parity + a loopback that proves the request/result contract
is serialization-clean (the detached seam), and composition with the background
monitor/wait_for primitive. Pure async, no LM/network.
"""

from __future__ import annotations

import pytest

from clio_agent.runtime.background_tasks import BackgroundTasks, TaskStatus
from clio_agent.runtime.expert_invoker import (
    ExpertEvent,
    ExpertRequest,
    ExpertResult,
    InProcessExpertInvoker,
    LoopbackExpertInvoker,
    spawn_invocation,
)


def _handler_factory(answer="ok"):
    async def handler(req: ExpertRequest) -> ExpertResult:
        return ExpertResult(
            expert_id=req.expert_id,
            answer=f"{answer}:{req.question}",
            events=[
                ExpertEvent("thought", {"text": "thinking"}),
                ExpertEvent("observation", {"text": f"saw {req.context.get('hint', '')}"}),
            ],
            workflow_state={"stage": "done", "n": 1},
        )

    return handler


def test_request_wire_roundtrip_is_lossless():
    req = ExpertRequest("data", "find X", session_id="s1", scope="agentA/data",
                        context={"hint": "h", "nested": {"a": [1, 2]}})
    assert ExpertRequest.from_wire(req.to_wire()) == req


def test_result_wire_roundtrip_is_lossless():
    res = ExpertResult("data", answer="A", status="completed",
                       events=[ExpertEvent("thought", {"t": 1})],
                       workflow_state={"k": "v"})
    assert ExpertResult.from_wire(res.to_wire()) == res


async def test_inprocess_invoker_is_parity_with_handler():
    handler = _handler_factory()
    req = ExpertRequest("data", "q", context={"hint": "H"})
    direct = await handler(req)
    via = await InProcessExpertInvoker(handler).invoke(req)
    assert via == direct


async def test_loopback_equals_inprocess_for_same_handler():
    handler = _handler_factory()
    req = ExpertRequest("data", "q", session_id="s1", scope="agentA/data",
                        context={"hint": "H"})
    in_proc = await InProcessExpertInvoker(handler).invoke(req)
    loop = await LoopbackExpertInvoker(handler).invoke(req)
    assert loop == in_proc  # detached seam is behavior-identical
    # and the events/workflow_state survived the wire
    assert [e.kind for e in loop.events] == ["thought", "observation"]
    assert loop.workflow_state == {"stage": "done", "n": 1}
    assert "H" in loop.events[1].payload["text"]


async def test_loopback_rejects_non_serializable_context():
    handler = _handler_factory()
    # a non-JSON value in the request context must fail at the seam, exactly as a
    # real detached transport would — surfacing it here, not at the far end.
    bad = ExpertRequest("data", "q", context={"obj": object()})
    with pytest.raises(TypeError):
        await LoopbackExpertInvoker(handler).invoke(bad)


async def test_spawn_invocation_runs_as_monitored_background_task():
    handler = _handler_factory()
    tasks = BackgroundTasks()
    req = ExpertRequest("data", "q", context={"hint": "H"})
    tid = spawn_invocation(tasks, LoopbackExpertInvoker(handler), req, label="data")
    rec = await tasks.wait(tid, timeout=2)
    assert rec.status is TaskStatus.COMPLETED
    result: ExpertResult = rec.result
    assert result.answer == "ok:q"
    # child events surfaced as incremental output for a status poll
    assert tasks.poll_output(tid) == ["thought", "observation"]


async def test_failing_handler_marks_task_failed():
    async def boom(req: ExpertRequest) -> ExpertResult:
        raise RuntimeError("child exploded")

    tasks = BackgroundTasks()
    tid = spawn_invocation(tasks, InProcessExpertInvoker(boom), ExpertRequest("x", "q"))
    rec = await tasks.wait(tid, timeout=2)
    assert rec.status is TaskStatus.FAILED
    assert "child exploded" in (rec.error or "")
