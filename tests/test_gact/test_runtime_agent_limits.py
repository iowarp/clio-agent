"""Runtime inference and depth settings over the production conversation owner."""

from __future__ import annotations

import asyncio
import threading
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.protocol.v3.event import _task_path
from clio_agent.gact.provenance.child_projection import child_session_lineage
from clio_agent.gact.session_descendants import descendant_session_ids
from clio_agent.gact.task_access import TaskCaller
from tests._config_layer import set_config
from tests.test_gact.task_records import record_delegation_fixture


def _save(client: TestClient, changes: dict[str, int | None]) -> dict[str, Any]:
    current = client.get("/v1/settings/runtime")
    assert current.status_code == 200, current.text
    saved = client.patch(
        "/v1/settings/runtime", json={"revision": current.json()["revision"], "changes": changes}
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def _children(app: Any, sid: str, count: int = 4, fanout: int | None = None) -> list[str]:
    from clio_agent.arc.task_types import AssignmentDetails

    children = []
    for index in range(count):
        task = record_delegation_fixture(app, sid, handle=f"task_limit_{index}")
        if fanout:
            details = AssignmentDetails.model_validate(
                app.state.task_history.read_content(task.details)
            )
            details = app.state.task_history.write_content(
                task.owner.session_id,
                details.model_copy(update={"inference_concurrency": fanout}).model_dump(
                    mode="json"
                ),
            )
            app.state.task_history.record_snapshot(
                task.model_copy(update={"details": details}), observation_id=f"limit-{index}"
            )
        children.append(task.owner.session_id)
    return children


def _held_calls(
    app: Any, client: TestClient, children: list[str]
) -> tuple[Any, list[threading.Event], Any]:
    """Run real inference admission while test events hold only model-call bodies."""
    entered = [threading.Event() for _ in children]
    releases: list[asyncio.Event] = []
    ready = threading.Event()

    async def run() -> None:
        releases.extend(asyncio.Event() for _ in children)

        async def call(index: int, sid: str) -> None:
            async with app.state.conversations.inference_slot(sid):
                entered[index].set()
                await releases[index].wait()

        workers = [asyncio.create_task(call(index, sid)) for index, sid in enumerate(children)]
        ready.set()
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    assert client.portal is not None
    future = client.portal.start_task_soon(run)
    assert ready.wait(5)

    def release(index: int | None = None) -> None:
        async def apply() -> None:
            for event in releases if index is None else [releases[index]]:
                event.set()

        client.portal.call(apply)

    return future, entered, release


def test_saved_unlimited_concurrency_is_used_by_inference(
    world: tuple[Any, TestClient, str],
) -> None:
    app, client, sid = world
    set_config("agent_tasks.max_concurrent", 0)
    children = _children(app, sid)
    future, entered, release = _held_calls(app, client, children)
    try:
        assert all(event.wait(5) for event in entered)
    finally:
        release()
        future.result(5)


def test_live_concurrency_increase_starts_waiting_calls_and_decrease_drains(
    world: tuple[Any, TestClient, str],
) -> None:
    app, client, sid = world
    _save(client, {"agent_tasks.max_concurrent": 1})
    future, entered, release = _held_calls(app, client, _children(app, sid))
    try:
        assert entered[0].wait(5)
        assert not any(event.is_set() for event in entered[1:])
        _save(client, {"agent_tasks.max_concurrent": 3})
        assert entered[1].wait(5) and entered[2].wait(5)
        assert not entered[3].is_set()
        _save(client, {"agent_tasks.max_concurrent": 1})
        release(0)
        release(1)
        assert not entered[3].wait(0.1)
        release(2)
        assert entered[3].wait(5)
    finally:
        release()
        future.result(5)


@pytest.mark.parametrize("fanout_bound,expected_running", [(None, 4), (2, 2)])
def test_unlimited_depth_capacity_preserves_parent_fanout(
    world: tuple[Any, TestClient, str],
    fanout_bound: int | None,
    expected_running: int,
) -> None:
    app, client, sid = world
    _save(client, {"agent_tasks.max_concurrent": 0})
    future, entered, release = _held_calls(app, client, _children(app, sid, fanout=fanout_bound))
    try:
        assert all(event.wait(5) for event in entered[:expected_running])
        assert sum(event.is_set() for event in entered) == expected_running
    finally:
        release()
        future.result(5)


def test_depth_updates_and_unlimited_preserve_deep_history_and_task_paths(
    world: tuple[Any, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, root = world
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "held_test_segment")
    tasks = []
    parent = root

    def spawn(invocation: str) -> dict[str, Any]:
        caller = TaskCaller(parent, app.state.human_principal)
        assert client.portal is not None
        return client.portal.call(
            partial(
                app.state.conversations.spawn,
                caller,
                "Keep the original assignment",
                "self",
                invocation,
                start=False,
            )
        )

    _save(client, {"agent_tasks.max_depth": 1})
    first = spawn("depth-first")
    tasks.append(first)
    parent = first["session_id"]
    with pytest.raises(ValueError, match="depth"):
        spawn("depth-rejected")
    _save(client, {"agent_tasks.max_depth": 0})
    for depth in range(2, 13):
        task = spawn(f"depth-{depth}")
        tasks.append(task)
        parent = task["session_id"]
    _save(client, {"agent_tasks.max_depth": 2})
    assert descendant_session_ids(app, root) == [task["session_id"] for task in tasks]
    lineage, truncated = child_session_lineage(app, root)
    root_task = app.state.task_history.latest_assignment(root)
    assert root_task is not None and root_task.kind == "Session"
    handles = [task["handle"] for task in tasks]
    assert not truncated and lineage[-1]["depth"] == 12
    assert lineage[-1]["task_path"] == handles
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    assert _task_path({"task_id": handles[-1]}) == [root_task.handle, *handles]


def test_runtime_limit_settings_validate_zero_and_reject_invalid_updates(
    world: tuple[Any, TestClient, str],
) -> None:
    _app, client, _sid = world
    snapshot = _save(client, {"agent_tasks.max_concurrent": 0, "agent_tasks.max_depth": 0})
    for row in snapshot["settings"]:
        if row["key"].startswith("agent_tasks."):
            assert row["value"] == row["unlimited_value"] == 0
            assert row["kind"] == "integer"
    for invalid in (-1, 1.5, True, "unlimited"):
        response = client.patch(
            "/v1/settings/runtime",
            json={"revision": snapshot["revision"], "changes": {"agent_tasks.max_depth": invalid}},
        )
        assert response.status_code == 422
    restored = _save(client, {"agent_tasks.max_concurrent": None, "agent_tasks.max_depth": None})
    assert (
        next(
            row["value"]
            for row in restored["settings"]
            if row["key"] == "agent_tasks.max_concurrent"
        )
        == 3
    )
