"""Execution joins wait for the task runtime's human principal (transcript boot window)."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.tool_execution_projection import own_execution_tasks


class _UnreadHistory:
    """Any read would mean a join was authorized without a caller."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"task history read before a principal exists: {name}")


def test_no_joins_before_the_runtime_admits_its_principal() -> None:
    app = SimpleNamespace(state=SimpleNamespace(task_history=_UnreadHistory()))
    assert own_execution_tasks(app, "sess", {"call_1"}) == {}


def test_no_joins_without_task_history() -> None:
    app = SimpleNamespace(state=SimpleNamespace(human_principal="human:1"))
    assert own_execution_tasks(app, "sess", {"call_1"}) == {}
