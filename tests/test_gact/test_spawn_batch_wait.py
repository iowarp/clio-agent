"""spawn_agents_parallel(wait=True): every child's report in the same call, each sized."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import spawn_batch_wait
from clio_agent.gact.task_result_spill import shared_result_bound, task_result_inline_chars
from tests._config_layer import set_config


def test_collected_results_share_the_result_bound() -> None:
    set_config(
        "limits", {"model_tool_result_chars": 12_000, "agent_task_output_digest_chars": 8_000}
    )
    assert task_result_inline_chars() == 8_000
    with shared_result_bound(4):
        assert task_result_inline_chars() == (12_000 - 1_000) // 4
    with shared_result_bound(40):
        assert task_result_inline_chars() == 1_500  # never below the floor
    with shared_result_bound(1):
        assert task_result_inline_chars() == 8_000
    assert task_result_inline_chars() == 8_000


@pytest.mark.asyncio
async def test_the_batch_waits_for_its_children_and_reports_the_ones_that_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_config(
        "limits", {"model_tool_result_chars": 12_000, "agent_task_output_digest_chars": 8_000}
    )
    seen: dict[str, Any] = {}

    async def wait(caller: Any, tasks: list[str], return_when: str, timeout_s: Any, **kw: Any):
        seen.update(tasks=tasks, return_when=return_when, bound=task_result_inline_chars(), **kw)
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
    assert seen["bound"] == (12_000 - 1_000) // 2 and seen["interrupt_on_mail"] is True
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
