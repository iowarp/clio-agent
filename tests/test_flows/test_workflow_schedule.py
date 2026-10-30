"""Workflow scheduling that the data flow adds on top of replay.

The fold, the gate and the cap are pure functions of the definition and the
recorded child results. These tests drive the workflow flow with scripted
calls so completion order is chosen by the test.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.flows.outcomes import CallCancelled, FlowStall
from clio_agent.flows.values import CallResult
from clio_agent.flows.workflow_fold import cancellation_is_deadline
from clio_agent.sdk.workflow import Workflow, WorkflowError

_FLOW_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/clio_agent/plugins/builtin/core/ai.iowarp.clio/flows/workflow.py"
)


def _flow_module() -> Any:
    spec = importlib.util.spec_from_file_location("clio_core_workflow_flow", _FLOW_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptedCall:
    def __init__(self, key: str) -> None:
        self.key = key
        self.invocation = key
        self.execution_deadline: str | None = None
        self.label = ""
        self.model = ""
        self.workflow_step: dict[str, Any] | None = None
        self.editor: dict[str, Any] | None = None
        self._future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def finish(self, result: CallResult) -> None:
        self._future.set_result(result)

    def __await__(self) -> Any:
        return self._future.__await__()


class ScriptedCtx:
    def __init__(self) -> None:
        self.started: list[ScriptedCall] = []
        self.events: list[dict[str, Any]] = []
        self.workflow_state: dict[str, Any] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.resolution = SimpleNamespace(flows=())
        self.run_id = "run-1"

    def call(self, target: Any, input: Any = None, *, key: str = "", **kwargs: Any) -> ScriptedCall:
        del kwargs
        call = ScriptedCall(key)
        call.input = input
        call.kind = "tool" if str(target) == "shell" else "agent"
        self.started.append(call)
        return call

    async def step(self, fn: Any, *args: Any) -> Any:
        if getattr(fn, "__name__", "") == "_bounds":
            return {"max_added_steps": None, "max_edit_rounds": None}
        return fn(*args)

    async def publish(self, view: dict[str, Any]) -> None:
        del view

    async def record_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def wait_calls(self, count: int) -> ScriptedCall:
        for _ in range(100):
            if len(self.started) >= count:
                return self.started[count - 1]
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} calls, saw {[call.key for call in self.started]}")


def _child(handle: str, **state: Any) -> CallResult:
    return CallResult({"answer": handle, "workflow_state": state}, handle=handle, target=handle)


def test_a_deadline_cancel_is_at_or_after_the_stored_deadline() -> None:
    assert cancellation_is_deadline("2026-10-30T00:00:02+00:00", "2026-10-30T00:00:02+00:00")
    assert not cancellation_is_deadline("2026-10-30T00:00:01+00:00", "2026-10-30T00:00:02+00:00")
    assert not cancellation_is_deadline(None, "2026-10-30T00:00:02+00:00")


def test_workflow_rejects_a_malformed_gate_cap_or_deadline() -> None:
    with pytest.raises(WorkflowError, match="exactly one"):
        Workflow.from_dict({"steps": [{"id": "a", "target": "t", "when_state": {"flag": {}}}]})
    with pytest.raises(WorkflowError, match="max_concurrency"):
        Workflow.from_dict({"steps": [{"id": "a", "target": "t"}], "max_concurrency": True})
    with pytest.raises(WorkflowError, match="timeout_s"):
        Workflow.from_dict({"steps": [{"id": "a", "target": "t", "timeout_s": -1}]})


@pytest.mark.concurrency
async def test_an_unset_cap_starts_every_ready_step() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    task = asyncio.create_task(
        module.WorkflowFlow().forward(
            {
                "workflow": {
                    "steps": [
                        {"id": "a", "target": "writer"},
                        {"id": "b", "target": "writer"},
                        {"id": "c", "target": "writer"},
                        {"id": "d", "target": "writer"},
                    ]
                }
            },
            ctx,
        )
    )
    await ctx.wait_calls(4)
    assert [call.key for call in ctx.started] == ["step:a", "step:b", "step:c", "step:d"]
    for call in ctx.started:
        call.finish(_child(call.key))
    await asyncio.wait_for(task, 2)


@pytest.mark.concurrency
async def test_max_concurrency_starts_the_next_step_after_the_first_finishes() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    task = asyncio.create_task(
        module.WorkflowFlow().forward(
            {
                "workflow": {
                    "max_concurrency": 1,
                    "steps": [
                        {"id": "a", "target": "writer"},
                        {"id": "b", "target": "writer"},
                    ],
                }
            },
            ctx,
        )
    )
    first = await ctx.wait_calls(1)
    assert [call.key for call in ctx.started] == ["step:a"]
    first.finish(_child("ha", one=True))
    second = await ctx.wait_calls(2)
    assert second.key == "step:b"
    second.finish(_child("hb", two=True))
    result = await asyncio.wait_for(task, 2)
    assert result["workflow_state"] == {"one": True, "two": True}


@pytest.mark.concurrency
@pytest.mark.parametrize("finish_first", ["a", "b"])
async def test_workflow_state_folds_in_definition_order(finish_first: str) -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    task = asyncio.create_task(
        module.WorkflowFlow().forward(
            {
                "workflow": {
                    "steps": [
                        {"id": "a", "target": "writer"},
                        {"id": "b", "target": "writer"},
                    ]
                }
            },
            ctx,
        )
    )
    await ctx.wait_calls(2)
    by_key = {call.key: call for call in ctx.started}
    order = ["step:a", "step:b"] if finish_first == "a" else ["step:b", "step:a"]
    by_key[order[0]].finish(
        _child(
            "ha" if order[0].endswith("a") else "hb",
            shared="from-a" if order[0].endswith("a") else "from-b",
        )
    )
    await asyncio.sleep(0)
    by_key[order[1]].finish(
        _child(
            "hb" if order[1].endswith("b") else "ha",
            shared="from-b" if order[1].endswith("b") else "from-a",
        )
    )
    result = await asyncio.wait_for(task, 2)
    assert result["workflow_state"]["shared"] == "from-b"
    assert result["conflicts"] == [{"key": "shared", "earlier": "ha", "later": "hb", "index": 1}]
    assert ctx.events[-1]["event"] == "workflow_state_conflict"
    assert not hasattr(ctx, "_state_sources")


async def test_a_gate_reads_its_dependencies_and_stalls_when_nothing_can_run() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    with pytest.raises(FlowStall) as raised:
        await module.WorkflowFlow().forward(
            {
                "workflow": {
                    "steps": [
                        {
                            "id": "a",
                            "target": "writer",
                            "when_state": {"flag.ready": {"equals": True}},
                        }
                    ]
                }
            },
            ctx,
        )
    assert raised.value.stall == "predicate_unsatisfied"
    assert raised.value.details["step"] == "a"
    assert ctx.started == []


@pytest.mark.concurrency
async def test_a_gate_ignores_an_unrelated_sibling_that_finishes_first() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    task = asyncio.create_task(
        module.WorkflowFlow().forward(
            {
                "workflow": {
                    "steps": [
                        {"id": "a", "target": "writer"},
                        {"id": "b", "target": "writer"},
                        {
                            "id": "c",
                            "target": "reader",
                            "after": ["a"],
                            "when_state": {"shared": {"equals": "from-a"}},
                        },
                    ]
                }
            },
            ctx,
        )
    )
    await ctx.wait_calls(2)
    by_key = {call.key: call for call in ctx.started}
    by_key["step:b"].finish(_child("hb", shared="from-b"))
    await asyncio.sleep(0)
    assert "step:c" not in {call.key for call in ctx.started}
    by_key["step:a"].finish(_child("ha", shared="from-a"))
    reader = await ctx.wait_calls(3)
    assert reader.key == "step:c"
    reader.finish(_child("hc", consumed=True))
    result = await asyncio.wait_for(task, 2)
    assert result["workflow_state"]["shared"] == "from-b"
    assert result["workflow_state"]["consumed"] is True


async def _offer(stall: str | None) -> str:
    ctx = ScriptedCtx()
    module = _flow_module()
    workflow = Workflow.from_dict({"steps": [{"id": "a", "target": "writer"}]})
    run = module._WorkflowRun(ctx, workflow, {"editors": ["critic"]})
    run.errors["a"] = {"stall": stall} if stall else {"reason": "call_failed"}
    offer = asyncio.create_task(run._offer_stall("a", CallCancelled("ended", handle="ha")))
    call = await ctx.wait_calls(1)
    offered = call.input["stall"]["stall"]
    call.finish(CallResult({"answer": "none"}, handle="editor"))
    await asyncio.wait_for(offer, 2)
    return offered


async def test_a_deadline_is_offered_to_editors_as_step_timeout() -> None:
    assert await _offer("step_timeout") == "step_timeout"
    assert await _offer(None) == "step_failed"


async def test_a_tool_deadline_stalls_instead_of_being_ignored() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    with pytest.raises(FlowStall) as raised:
        await module.WorkflowFlow().forward(
            {"workflow": {"steps": [{"id": "a", "target": "shell", "timeout_s": 1}]}},
            ctx,
        )
    assert raised.value.stall == "timeout_requires_task"
    assert raised.value.details["step"] == "a"
    assert ctx.started[0].execution_deadline is None


async def test_a_step_deadline_is_passed_on_the_call() -> None:
    ctx = ScriptedCtx()
    module = _flow_module()
    task = asyncio.create_task(
        module.WorkflowFlow().forward(
            {"workflow": {"steps": [{"id": "a", "target": "writer", "timeout_s": 5}]}},
            ctx,
        )
    )
    call = await ctx.wait_calls(1)
    assert call.execution_deadline is not None
    assert call.execution_deadline.endswith("+00:00")
    call.finish(_child("ha", done=True))
    await asyncio.wait_for(task, 2)
