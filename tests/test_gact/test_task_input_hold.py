"""A task-augmented call holds while its task waits for an answer it asked for."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import task_input_hold
from clio_agent.gact.question_titles import (
    ASK_LIMIT,
    GENERIC_TITLE,
    question_asker,
    question_title,
)
from clio_agent.plugins import host_semantics


class _IO:
    async def run(self, fn: Any, *args: Any) -> Any:
        return fn(*args)


class _History:
    def __init__(self, status: str) -> None:
        self.io = _IO()
        self.task = SimpleNamespace(terminal_at=None, status=status, activity="", raw_status=status)
        self._wakes: list[Any] = []

    def listen(self, callback: Any, *, handles: tuple[str, ...]) -> Any:
        self._wakes.append(callback)
        return lambda: self._wakes.remove(callback)

    def task_snapshot(self, _handle: str) -> Any:
        return self.task

    def report(self, **updates: Any) -> None:
        self.task = SimpleNamespace(**{**vars(self.task), **updates})
        for wake in list(self._wakes):
            wake(1)


class _Operations:
    def __init__(self) -> None:
        self.stopped: set[str] = set()

    def assignment_stopped(self, handle: str) -> bool:
        return handle in self.stopped


def _app(history: _History) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(task_history=history, task_operations=_Operations())
    )


async def _hold(app: Any, **kwargs: Any) -> bool:
    return await task_input_hold.hold_while_input(
        app, "task-1", assignment="assign-1", session_id="sid", **kwargs
    )


@pytest.mark.asyncio
async def test_a_reported_running_task_returns_at_once() -> None:
    app = _app(_History("running"))

    held = await asyncio.wait_for(_hold(app, reported=lambda: True, first_report_s=30), 1)

    assert held is False


@pytest.mark.asyncio
async def test_an_unreported_task_waits_only_for_the_first_report() -> None:
    app = _app(_History("running"))

    held = await asyncio.wait_for(_hold(app, reported=lambda: False, first_report_s=0.05), 1)

    assert held is False


@pytest.mark.asyncio
async def test_an_ask_holds_the_call_until_it_is_answered() -> None:
    history = _History("running")
    app = _app(history)
    seen = {"reported": False}
    call = asyncio.create_task(_hold(app, reported=lambda: seen["reported"], first_report_s=5))
    await asyncio.sleep(0.02)
    seen["reported"] = True
    history.report(status="input_required", activity="waiting_for_input")
    await asyncio.sleep(0.05)
    assert not call.done()

    history.report(status="running", activity="executing")

    assert await asyncio.wait_for(call, 1) is True


@pytest.mark.asyncio
async def test_an_ask_releases_the_call_when_the_task_settles() -> None:
    history = _History("input_required")
    call = asyncio.create_task(_hold(_app(history)))
    await asyncio.sleep(0.05)
    assert not call.done()

    history.report(terminal_at="now", status="completed")

    assert await asyncio.wait_for(call, 1) is True


@pytest.mark.asyncio
async def test_a_stopped_turn_releases_a_held_call() -> None:
    history = _History("input_required")
    app = _app(history)
    call = asyncio.create_task(_hold(app))
    await asyncio.sleep(0.05)
    assert not call.done()

    app.state.task_operations.stopped.add("assign-1")
    history.report()

    assert await asyncio.wait_for(call, 1) is True


@pytest.fixture
def _remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_semantics, "_BY_PLUGIN", {})
    host_semantics.remember(
        {
            "runner": {"plugin": "jobs", "host": {"title": "Batch jobs"}},
            "bare": {"plugin": "untitled", "host": {}},
        }
    )


@pytest.mark.usefixtures("_remembered")
def test_a_server_question_is_titled_by_its_first_sentence_and_names_its_plugin() -> None:
    metadata = {"elicitation": {"namespace": "runner"}}
    prompt = "## Submit the job to **cluster-a**? It will use 4 nodes.\n\nDetails follow."

    assert question_title(prompt, metadata) == "Submit the job to cluster-a?"
    assert question_title("", metadata) == GENERIC_TITLE
    assert question_asker(metadata) == "Batch jobs"


@pytest.mark.usefixtures("_remembered")
def test_an_asker_falls_back_to_the_server_name_and_a_title_stays_bounded() -> None:
    long_ask = "Review " + "the package manifest and its build recipe " * 6
    metadata = {"elicitation": {"namespace": "bare"}}
    title = question_title(long_ask, metadata)

    assert title.startswith("Review the package")
    assert title.endswith("\u2026")
    assert len(title) <= ASK_LIMIT
    assert "\n" not in title
    assert question_asker(metadata) == "bare"


def test_a_url_question_uses_its_recorded_plugin_title_and_others_stay_generic() -> None:
    url = {"elicitation": {"server": "s", "plugin_title": "Sign-in helper"}}

    assert question_title("Open the sign-in page.", url) == "Open the sign-in page."
    assert question_asker(url) == "Sign-in helper"
    assert question_title("Pick one", {}) == GENERIC_TITLE
    assert question_asker({}) == ""
