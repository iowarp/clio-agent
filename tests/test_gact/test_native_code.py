"""Background ``run_code``: an approved script runs as owned work on the code root (CT2).

The REAL app and task layer: the script is staged, submitted through the builtin boundary
to the code native root, runs in a kernel of its own (not the scope's), and settles as a
task; cancelling the task stops its kernel. The agent's tools are the code-mode test
agent's (records), so nested calls go through the real dispatcher.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import native_code
from clio_agent.gact.app import build_app
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.task_access import TaskCaller
from clio_agent.tools.execution import tool_workspace_context
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture
from tests.test_gact.test_code_mode import _agent

pytestmark = [pytest.mark.usefixtures("host_agent_executor"), pytest.mark.timeout(120)]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Any, str, Path]]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        root = tmp_path / "work"
        root.mkdir()
        wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()
        sid = client.post("/v1/sessions", json={"workspace_id": wid["id"]}).json()["id"]
        record_assignment_fixture(app, sid)
        yield app, sid, root
        kernels.close_session(sid)


def _launch(app: Any, sid: str, root: Path, code: str, label: str = "") -> dict[str, Any]:
    with active_turn_fixture(app, sid), tool_workspace_context(str(root)):
        return tool_module.launch_background(
            app, _agent(), code, label, session_id=sid, read_only=False
        )


def _settled(app: Any, handle: str, wait_s: float = 60) -> Any:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        task = app.state.task_history.task_snapshot(handle)
        if task is not None and task.terminal_at is not None:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {handle} did not settle")


def test_a_background_script_runs_as_a_task_in_its_own_kernel(world: Any) -> None:
    """Accepted at once with a Code task handle; the script's nested calls run; the task
    settles completed; its kernel was its own and is gone after it settles.

    SABOTAGE: run the background script in the scope's kernel (key ``(sid, scope)``) ->
    the kernel-key assertion fails."""
    app, sid, root = world
    code = (
        "import asyncio\n"
        "rows = await asyncio.gather(*(tools.records.get_record(record_id=i) "
        "for i in ('r1', 'r2', 'r3')))\n"
        "print('read', len(rows))\n"
        "len(rows)"
    )
    keys_seen: list[tuple[str, str]] = []
    real = kernels.kernel_for

    def spy(session_id: str, scope: str, *args: Any, **kwargs: Any) -> Any:
        keys_seen.append((session_id, scope))
        return real(session_id, scope, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kernels, "kernel_for", spy)
        receipt = _launch(app, sid, root, code, label="read three records")
        handle = str(receipt["handle"])
        assert handle.startswith("task_")
        task = _settled(app, handle)
    assert task.kind == "Code" and task.status == "completed", task
    assert keys_seen == [(sid, "task:" + handle)]
    assert (sid, "task:" + handle) not in kernels._KERNELS
    assert not native_code.staged(str(receipt.get("launch") or "x"))


def test_cancelling_a_background_script_stops_its_kernel(world: Any) -> None:
    """A script that would run for minutes stops when its task is cancelled.

    SABOTAGE: ignore the stop event in the worker -> the task never settles -> red."""
    app, sid, root = world
    receipt = _launch(app, sid, root, "import asyncio\nawait asyncio.sleep(600)")
    handle = str(receipt["handle"])
    deadline = time.monotonic() + 30
    while (sid, "task:" + handle) not in kernels._KERNELS and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (sid, "task:" + handle) in kernels._KERNELS
    cancel = app.state.task_operations.cancel(TaskCaller(sid, app.state.human_principal), handle)
    boundary = app.state.builtin_boundary
    import asyncio

    asyncio.run_coroutine_threadsafe(cancel, boundary._loop).result(timeout=30)
    task = _settled(app, handle)
    assert task.status == "cancelled", task
    assert (sid, "task:" + handle) not in kernels._KERNELS


def test_only_an_approved_staged_launch_reaches_the_code_root(world: Any) -> None:
    """The boundary refuses a code task without a staged (approved) launch."""
    app, sid, _root = world
    import asyncio
    import contextvars

    boundary = app.state.builtin_boundary
    with active_turn_fixture(app, sid):
        captured = contextvars.copy_context()
    coroutine = boundary.native_call(
        "run_code", {"code": "1", "launch": "launch_forged", "label": ""}, captured
    )
    with pytest.raises(PermissionError, match="approved launch"):
        asyncio.run_coroutine_threadsafe(coroutine, boundary._loop).result(timeout=30)


def test_a_running_script_reports_elapsed_time_calls_and_memory(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task row shows a running script's state, elapsed time, nested calls and the
    kernel's memory (CT3: memory is shown, never a limit).

    SABOTAGE: drop the status reporter -> no status row while it runs -> red."""
    app, sid, root = world
    monkeypatch.setenv("CLIO_CODE_BACKGROUND_STATUS_S", "0.5")
    code = (
        "import asyncio\n"
        "for _ in range(5):\n"
        "    await tools.records.get_record(record_id='r1')\n"
        "await asyncio.sleep(4)"
    )
    receipt = _launch(app, sid, root, code)
    handle = str(receipt["handle"])
    seen: dict[str, Any] = {}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not seen:
        task = app.state.task_history.task_snapshot(handle)
        reference = task.progress.output if task is not None else None
        if reference is not None:
            row = app.state.task_history.read_content(reference).get("output")
            if isinstance(row, dict) and row.get("type") == "clio.code.status":
                seen = row
        time.sleep(0.1)
    assert seen, "no status row while the script ran"
    assert seen["state"] == "running" and seen["elapsed_s"] > 0
    assert seen["calls"] == 5
    assert isinstance(seen["memory_mb"], float) and seen["memory_mb"] > 5
    assert _settled(app, handle).status == "completed"
