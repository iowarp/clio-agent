"""spawn_agents_parallel(wait=True): every child's report in the same call, whole."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import child_report_fit, spawn_batch_wait


@pytest.mark.asyncio
async def test_the_batch_waits_for_its_children_and_reports_the_ones_that_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child_report_fit, "_measure", lambda: child_report_fit._Room(9_000))
    seen: dict[str, Any] = {}

    async def wait(caller: Any, tasks: list[str], return_when: str, timeout_s: Any, **kw: Any):
        seen.update(tasks=tasks, return_when=return_when, room=child_report_fit._ROOM.get(), **kw)
        return {"outcomes": [{"handle": h, "pending": False, "result": "done"} for h in tasks]}

    async def run(function: Any, *args: Any) -> Any:
        return function(*args)

    app = SimpleNamespace(
        state=SimpleNamespace(
            task_operations=SimpleNamespace(wait=wait),
            task_history=SimpleNamespace(io=SimpleNamespace(run=run)),
        )
    )
    monkeypatch.setattr("clio_agent.gact.task_declarations._runtime", lambda: (app, object()))
    monkeypatch.setattr(
        "clio_agent.gact.task_tool_collection.collected", lambda app, caller, value: value
    )
    value = await spawn_batch_wait.wait_for_batch(
        [{"handle": "task_a"}, {"index": 1, "error": "no such agent"}, {"handle": "task_b"}]
    )
    assert seen["tasks"] == ["task_a", "task_b"] and seen["return_when"] == "all"
    # One room for the whole batch, measured once on the caller's context.
    assert seen["room"].tokens == 9_000 and seen["interrupt_on_mail"] is True
    assert [row["handle"] for row in value["outcomes"]] == ["task_a", "task_b"]
    assert value["not_started"] == [{"index": 1, "error": "no such agent"}]


def test_a_batch_that_waited_is_presented_as_one_wait() -> None:
    from clio_agent.gact.task_wait_presentation import wait_presentation

    view = wait_presentation(
        {"spawns": [{}, {}, {}], "wait": True},
        {
            "pending": [],
            "outcomes": [
                {"handle": "task_a", "pending": False, "status": "completed", "result": "A found"},
                {"handle": "task_b", "pending": False, "status": "completed", "result": "B found"},
            ],
            "not_started": [{"index": 1, "error": "no such agent"}],
        },
    )
    texts = [block.get("text") for block in view["blocks"]]
    assert "A found" in texts and "B found" in texts
    assert "Child 2 did not start: no such agent" in texts
    assert view["status"] == "degraded"
