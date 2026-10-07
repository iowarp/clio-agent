"""Background warm-ups drain and queue with real blueprint revision changes."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import session_warmup, workspace_warmup
from clio_agent.gact.blueprint_reload import apply_blueprint_change
from clio_agent.gact.turn_runner import TurnRunner


def _app() -> Any:
    lock = threading.Lock()
    leases: dict[str, int] = {}
    agent = SimpleNamespace(
        _active_tool_executor=lambda: None,
        _workspace_state=lambda: (lock, {}, leases),
    )
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            turn_runner=TurnRunner({}),
            sessions=SimpleNamespace(list=lambda: []),
            session_defaults=SimpleNamespace(get=lambda: SimpleNamespace(blueprint_id="")),
            bus=SimpleNamespace(publish=lambda event: None),
        )
    )


async def _closed(app: Any) -> None:
    for _ in range(100):
        if not app.state.turn_runner.revision_gate._open.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("revision writer did not close the gate")


@pytest.mark.asyncio
async def test_blueprint_change_drains_session_warmup_fleet_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session creation followed by blueprint selection cannot fail on its warm-up."""
    app = _app()
    entered, finish = threading.Event(), threading.Event()
    seen: list[str] = []

    def warm(owner: Any, sid: str, trigger: str) -> None:
        lock, _, leases = owner.state.agent._workspace_state()
        with lock:
            leases["workspace"] = 1
        try:
            entered.set()
            assert finish.wait(5)
        finally:
            with lock:
                leases.clear()
        seen.append("warmup finished")

    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm", warm)
    worker = session_warmup.start_session_warmup(app, "drain", trigger="session_created")
    assert isinstance(worker, asyncio.Task)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        change = asyncio.create_task(
            apply_blueprint_change(
                app, lambda: seen.append("change") or {"installed": []}, label="Select blueprint"
            )
        )
        await _closed(app)
        assert seen == []
        finish.set()
        result = await asyncio.wait_for(change, timeout=5)
        await worker
        assert result["operation"]["status"] == "applied"
        assert seen == ["warmup finished", "change"]
    finally:
        finish.set()
        await worker


@pytest.mark.asyncio
async def test_workspace_warmup_queues_behind_blueprint_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A draft opened during a revision cannot acquire an old-policy fleet lease."""
    app = _app()
    entered, finish, warmed = threading.Event(), threading.Event(), threading.Event()
    seen: list[str] = []

    def mutation() -> dict[str, Any]:
        entered.set()
        assert finish.wait(5)
        seen.append("change")
        return {"installed": []}

    def warm(owner: Any, root: str, blueprint_id: str, key: tuple[int, str, str]) -> None:
        seen.append("warmup")
        workspace_warmup._release(key)
        warmed.set()

    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(workspace_warmup, "_warm_and_release", warm)
    change = asyncio.create_task(apply_blueprint_change(app, mutation, label="Reload"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert workspace_warmup.start_workspace_warmup(app, str(tmp_path)) == "warming"
        await asyncio.sleep(0)
        assert not warmed.is_set()
        finish.set()
        await asyncio.wait_for(change, timeout=5)
        assert await asyncio.to_thread(warmed.wait, 5)
        await asyncio.sleep(0)
        assert seen == ["change", "warmup"]
    finally:
        finish.set()
        await change


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_warmup_keeps_revision_reader_until_thread_exits(
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
) -> None:
    """Cancelling an async wrapper cannot let a revision outlive a leased worker."""
    app = _app()
    entered, finish = threading.Event(), threading.Event()
    seen: list[str] = []

    def warm(owner: Any, sid: str, trigger: str) -> None:
        lock, _, leases = owner.state.agent._workspace_state()
        with lock:
            leases["workspace"] = 1
        try:
            entered.set()
            assert finish.wait(5)
        finally:
            with lock:
                leases.clear()
        seen.append("warmup finished")

    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm", warm)
    worker = session_warmup.start_session_warmup(app, "cancel-running", trigger="session_created")
    assert isinstance(worker, asyncio.Task)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        worker.cancel()
        change = asyncio.create_task(
            apply_blueprint_change(
                app, lambda: seen.append("change") or {"installed": []}, label="Reload"
            )
        )
        await _closed(app)
        for _ in range(cancel_count - 1):
            worker.cancel()
            await asyncio.sleep(0)
        assert not worker.done()
        assert seen == []
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.wait_for(change, timeout=5)
        assert seen == ["warmup finished", "change"]
    finally:
        finish.set()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_running_turn_warmup_can_finish_while_writer_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn's descendant warm-up cannot deadlock a writer draining its parent."""
    app = _app()
    entered, proceed = asyncio.Event(), asyncio.Event()
    seen: list[str] = []
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm", lambda app, sid, trigger: seen.append("warmup"))

    async def turn() -> None:
        entered.set()
        await proceed.wait()
        worker = session_warmup.start_session_warmup(app, "descendant", trigger="turn_started")
        assert isinstance(worker, asyncio.Task)
        await worker
        seen.append("turn finished")

    parent = app.state.turn_runner.spawn(turn(), sid="parent", turn_id="parent")
    await entered.wait()
    change = asyncio.create_task(
        apply_blueprint_change(
            app, lambda: seen.append("change") or {"installed": []}, label="Reload"
        )
    )
    await _closed(app)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(parent, change), timeout=5)
    assert seen == ["warmup", "turn finished", "change"]


@pytest.mark.asyncio
@pytest.mark.parametrize("let_task_start", [False, True])
async def test_cancelled_queued_warmup_releases_its_coalescing_slot(
    monkeypatch: pytest.MonkeyPatch,
    let_task_start: bool,
) -> None:
    """A cancelled queued warm-up cannot permanently suppress later preparation."""
    app = _app()
    seen: list[str] = []
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm", lambda app, sid, trigger: seen.append("warmup"))
    async with app.state.turn_runner.revision_gate.change():
        worker = session_warmup.start_session_warmup(
            app, "cancel-queued", trigger="session_created"
        )
        assert isinstance(worker, asyncio.Task)
        if let_task_start:
            await asyncio.sleep(0)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert "cancel-queued" not in session_warmup._inflight
        assert seen == []
    retry = session_warmup.start_session_warmup(app, "cancel-queued", trigger="session_created")
    assert isinstance(retry, asyncio.Task)
    await retry
    assert seen == ["warmup"]


@pytest.mark.asyncio
async def test_completed_warmup_does_not_clear_its_successor_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old completion must not unguard a newer warm-up of the same session."""
    app = _app()
    loop = asyncio.get_running_loop()
    original_release = session_warmup._release
    second_entered, finish = threading.Event(), threading.Event()
    lock = threading.Lock()
    warmed = 0
    releases = 0
    successors: list[asyncio.Task[None]] = []

    async def successor() -> None:
        worker = session_warmup.start_session_warmup(app, "successor", trigger="turn_started")
        assert isinstance(worker, asyncio.Task)
        successors.append(worker)
        assert await asyncio.to_thread(second_entered.wait, 5)

    def release(sid: str) -> None:
        nonlocal releases
        original_release(sid)
        with lock:
            releases += 1
            first_release = releases == 1
        if first_release:
            asyncio.run_coroutine_threadsafe(successor(), loop).result(timeout=5)

    def warm(owner: Any, sid: str, trigger: str) -> None:
        nonlocal warmed
        with lock:
            warmed += 1
            second = warmed == 2
        if second:
            second_entered.set()
            assert finish.wait(5)

    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm", warm)
    monkeypatch.setattr(session_warmup, "_release", release)
    first = session_warmup.start_session_warmup(app, "successor", trigger="session_created")
    assert isinstance(first, asyncio.Task)
    try:
        await asyncio.wait_for(first, timeout=5)
        assert second_entered.is_set()
        assert "successor" in session_warmup._inflight
        assert session_warmup.start_session_warmup(app, "successor", trigger="turn_started") is None
    finally:
        finish.set()
        await asyncio.gather(first, *successors, return_exceptions=True)
