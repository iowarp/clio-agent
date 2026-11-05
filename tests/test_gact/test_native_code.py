"""Background ``run_code``: an approved script runs as owned work on the code root (CT2).

The REAL app and task layer: the script is staged, submitted through the builtin boundary
to the code native root, runs in a kernel of its own (not the scope's), and settles as a
task; cancelling the task stops its kernel. The agent's tools are the code-mode test
agent's (records), so nested calls go through the real dispatcher.
"""

from __future__ import annotations

import json
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
            if (
                isinstance(row, dict)
                and row.get("type") == "clio.code.status"
                and row.get("calls") == 5  # under load the first report can precede the calls
            ):
                seen = row
        time.sleep(0.1)
    assert seen, "no status row while the script ran"
    assert seen["state"] == "running" and seen["elapsed_s"] > 0
    assert seen["calls"] == 5
    assert isinstance(seen["memory_mb"], float) and seen["memory_mb"] > 5
    assert _settled(app, handle).status == "completed"


def test_task_progress_reaches_the_row_and_wakes_nobody(world: Any) -> None:
    """``task.progress`` writes the task row's progress with the script's status.

    SABOTAGE: drop the reserved progress handler -> the script's call raises -> red."""
    app, sid, root = world
    code = (
        "await task.progress('half way', done=1, total=2)\nimport asyncio\nawait asyncio.sleep(1)"
    )
    handle = str(_launch(app, sid, root, code)["handle"])
    found: dict[str, Any] = {}
    line = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not found:
        task = app.state.task_history.task_snapshot(handle)
        reference = task.progress.output if task is not None else None
        if reference is not None:
            row = app.state.task_history.read_content(reference).get("output")
            if isinstance(row, dict) and row.get("type") == "clio.code.progress":
                found, line, unit = row, task.progress.message, task.progress.unit
        time.sleep(0.05)
    assert found["message"] == "half way" and (found["done"], found["total"]) == (1, 2)
    assert "memory_mb" in found and found["state"] == "running"
    # The row reads as the script's status, not as shell output (SABOTAGE: record code
    # rows like shell chunks in NativeProgress -> "Shell output available", unit bytes).
    assert line.endswith("1 of 2: half way") and " call" in line and unit == ""
    # Named by its first line when it has no label (SABOTAGE: label=context.tool in
    # NativeTools.submit -> "run_code_task").
    assert app.state.task_history.task_snapshot(handle).label == (
        "await task.progress('half way', done=1, total=2)"
    )
    assert _settled(app, handle).status == "completed"


def test_a_status_line_reads_time_calls_memory_and_the_last_progress() -> None:
    from clio_agent.gact.native_code import status_summary

    assert status_summary({"elapsed_s": 125.4, "calls": 1000, "memory_mb": 55.2}) == (
        "2 min 5 s, 1,000 calls, 55 MB"
    )
    row = {"elapsed_s": 7200, "calls": 1, "memory_mb": None, "message": "sensor 4", "done": 3}
    assert status_summary(row) == "2 h 0 min, 1 call, 3 done: sensor 4"


def test_notifies_inside_the_window_arrive_together_and_none_is_lost(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most one wake per window per conversation; queued notifies arrive at its end as
    one delivery, and the result counts them (CT9).

    SABOTAGE: send every notify at once (no window) -> three deliveries -> red."""
    from clio_agent.gact import code_task_reporting as reporting

    app, sid, root = world
    monkeypatch.setenv("CLIO_CODE_NOTIFY_MIN_INTERVAL_S", "1.5")
    monkeypatch.setattr(reporting, "_WINDOWS", {})
    sent: list[tuple[str, list[dict[str, Any]]]] = []
    monkeypatch.setattr(
        reporting, "_send", lambda _app, owner, entries: sent.append((owner, entries))
    )
    code = (
        "for word in ('one', 'two'):\n"
        "    await task.notify(word, data={'word': word})\n"
        "task.notify('three', data={'word': 'three'})\n"  # a forgotten await still sends
        "import asyncio\nawait asyncio.sleep(3)"
    )
    handle = str(_launch(app, sid, root, code, label="watcher")["handle"])
    task = _settled(app, handle)
    assert task.status == "completed"
    assert [len(entries) for _owner, entries in sent] == [1, 2]
    assert [e["message"] for _o, entries in sent for e in entries] == ["one", "two", "three"]
    assert all(owner == sid for owner, _entries in sent)
    assert sent[1][1][0]["handle"] == handle and sent[1][1][0]["label"] == "watcher"
    result = app.state.task_history.read_content(task.result)
    assert "notifies_coalesced" in json.dumps(result) and '"notifies_coalesced": 2' in json.dumps(
        result
    )


def test_a_last_notify_without_await_is_sent_and_waited_for(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call is made when the script makes it: a notify the script neither awaits nor
    outlives (the live CK1 watcher's last line) is delivered, and nothing is reported
    stopped.

    SABOTAGE: send the call from a task (``ensure_future``) -> the task is cancelled with
    the script and reported "stopped with it" -> red."""
    from clio_agent.gact import code_task_reporting as reporting

    app, sid, root = world
    monkeypatch.setattr(reporting, "_WINDOWS", {})
    sent: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(reporting, "_send", lambda _app, _owner, entries: sent.append(entries))
    handle = str(_launch(app, sid, root, "task.notify('done')", label="last")["handle"])
    task = _settled(app, handle)
    assert task.status == "completed"
    assert [e["message"] for entries in sent for e in entries] == ["done"]
    assert "stopped with it" not in json.dumps(app.state.task_history.read_content(task.result))


def test_a_synchronous_script_has_no_task_binding(world: Any) -> None:
    """``task`` exists only in a background kernel."""
    app, sid, root = world
    with active_turn_fixture(app, sid), tool_workspace_context(str(root)):
        outcome = tool_module.run_script(_agent(), "task", session_id=sid, scope="main")
    assert outcome["status"] == "failed" and "NameError" in outcome["error"]


def test_a_notify_is_attention_mail_attributed_to_its_task() -> None:
    """``_send`` writes one attention message for the owner, grouped by task."""
    from types import SimpleNamespace

    from clio_agent.gact import code_task_reporting as reporting

    written: dict[str, Any] = {}

    class History:
        def serialized(self) -> Any:
            import contextlib

            return contextlib.nullcontext()

        def write_content(self, session_id: str, value: object) -> Any:
            from clio_agent.arc.task_types import CoreReference

            written["content"] = (session_id, value)
            return CoreReference.model_construct()

        def record_mail(self, item: Any) -> Any:
            written["mail"] = item
            return item

    app = SimpleNamespace(state=SimpleNamespace(task_history=History()))
    entry = {"handle": "task_a", "label": "feed", "message": "hot", "data": {"t": 231}, "at": "x"}
    reporting._send(app, "sess_owner", [entry, {**entry, "message": "hotter"}])
    mail = written["mail"]
    assert (mail.kind, mail.recipient_session, mail.handle) == ("attention", "sess_owner", "task_a")
    owner, value = written["content"]
    assert owner == "sess_owner" and value["type"] == "clio.code.notify"
    assert [n["message"] for n in value["tasks"][0]["notifies"]] == ["hot", "hotter"]


def test_notifies_of_two_scripts_in_one_window_stay_attributed_to_each() -> None:
    """Every task's mail names its task by ``handle``, as a task's result does: two scripts
    notifying in the same window give two mails, one per task, recorded together.

    SABOTAGE: one grouped mail for the window (``handle=None`` when tasks differ) -> red."""
    from types import SimpleNamespace

    from clio_agent.gact import code_task_reporting as reporting

    mails: list[Any] = []

    class History:
        def serialized(self) -> Any:
            import contextlib

            return contextlib.nullcontext()

        def write_content(self, session_id: str, value: object) -> Any:
            from clio_agent.arc.task_types import CoreReference

            return CoreReference.model_construct()

        def record_mail(self, item: Any) -> Any:
            mails.append(item)
            return item

    app = SimpleNamespace(state=SimpleNamespace(task_history=History()))
    first = {"handle": "task_a", "label": "a", "message": "1", "data": None, "at": "x"}
    reporting._send(app, "sess_owner", [first, {**first, "handle": "task_b", "label": "b"}])
    assert sorted(m.handle for m in mails) == ["task_a", "task_b"]
    assert all(m.kind == "attention" for m in mails)


def test_a_background_scripts_output_streams_to_its_file(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output is written to the session's scratch file as the script prints, the status
    row names it, and the final result carries what was printed.

    SABOTAGE: drop output_path from the exec message -> no file while running -> red."""
    from pathlib import Path as _Path

    monkeypatch.setenv("CLIO_CODE_BACKGROUND_STATUS_S", "0.5")
    app, sid, root = world
    code = "print('first line', flush=True)\nimport asyncio\nawait asyncio.sleep(2)\nprint('last')"
    handle = str(_launch(app, sid, root, code)["handle"])
    path = ""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not path:
        task = app.state.task_history.task_snapshot(handle)
        reference = task.progress.output if task is not None else None
        if reference is not None:
            row = app.state.task_history.read_content(reference).get("output")
            if isinstance(row, dict) and row.get("output_bytes"):
                path = str(row["output_path"])
        time.sleep(0.1)
    assert path.endswith(f"code-task-{handle}.log")
    assert _Path(path).read_text().startswith("first line")  # written while it ran
    task = _settled(app, handle)
    result = json.dumps(app.state.task_history.read_content(task.result))
    assert "first line" in result and "last" in result
    assert _Path(path).read_text().splitlines() == ["first line", "last"]


def test_background_scripts_never_starve_each_others_calls() -> None:
    """CK3: each script runs on its own thread, so scripts holding their threads for
    their whole run cannot take the loop executor their nested calls need.

    SABOTAGE: run each script with ``loop.run_in_executor(None, ...)`` (the old
    ``asyncio.to_thread``) and the three scripts deadlock on the one-thread executor."""
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from clio_agent.gact.native_code import _on_own_thread

    async def main() -> list[str]:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))

        def script(n: int) -> str:
            # A nested call: work the loop's default executor runs, awaited from the script.
            done = asyncio.run_coroutine_threadsafe(asyncio.to_thread(time.sleep, 0.05), loop)
            done.result(timeout=10)
            return f"script {n} done"

        futures = [_on_own_thread(f"t{n}", lambda n=n: script(n)) for n in range(3)]
        return list(await asyncio.wait_for(asyncio.gather(*futures), timeout=15))

    assert asyncio.run(main()) == ["script 0 done", "script 1 done", "script 2 done"]


def test_a_background_script_runs_in_its_launchers_context() -> None:
    """The script's thread sees the launching context (the active session), as
    ``to_thread`` did: the kernel's sandbox territory includes the session's scratch
    folder only through it, and the script's output log lives there (live CK10: a kernel
    started without it could not open its log, so every background script failed).

    SABOTAGE: call ``run()`` directly on the new thread -> the variable is unset -> red."""
    import asyncio

    from clio_agent.gact import context as gact_context
    from clio_agent.gact.native_code import _on_own_thread
    from clio_agent.runtime.session_scratch import active_session

    async def main() -> object:
        gact_context.set_session_id("sess_context_check")
        return await _on_own_thread("t", active_session)

    assert asyncio.run(main()) == "sess_context_check"


def test_queued_notifies_are_sent_when_the_task_settles_before_its_result(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notify still queued in its window when the script ends is sent then, before the
    task settles (so before its result mail), not when the window ends.

    SABOTAGE: drop the ``notifies`` step of ``_script_ended`` -> "two" waits for the
    30 s window, after the result -> red."""
    from clio_agent.gact import code_task_reporting as reporting

    app, sid, root = world
    monkeypatch.setenv("CLIO_CODE_NOTIFY_MIN_INTERVAL_S", "30")
    monkeypatch.setattr(reporting, "_WINDOWS", {})
    sent: list[tuple[str, bool]] = []

    def send(_app: Any, _owner: str, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            task = app.state.task_history.task_snapshot(entry["handle"])
            sent.append((entry["message"], task is not None and task.terminal_at is None))

    monkeypatch.setattr(reporting, "_send", send)
    code = "await task.notify('one')\nawait task.notify('two')"
    handle = str(_launch(app, sid, root, code, label="pair")["handle"])
    task = _settled(app, handle)
    assert task.status == "completed"
    assert sent == [("one", True), ("two", True)]  # both before the task settled
    assert not reporting._WINDOWS[sid].pending
    assert reporting._WINDOWS[sid].timer is None  # nothing left to send later


def test_a_task_cancelled_before_its_worker_runs_leaves_no_staged_launch(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the ``take_launch`` in ``native_run_code``'s finally -> the approved
    script stays staged forever -> red."""
    from clio_agent.gact.native_work import NativeWork

    app, sid, root = world
    keys: list[str] = []
    real_stage = native_code.stage_launch

    def stage(launch: Any) -> str:
        keys.append(real_stage(launch))
        return keys[-1]

    async def refused(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("cancelled before admission")

    monkeypatch.setattr(native_code, "stage_launch", stage)
    monkeypatch.setattr(NativeWork, "run", refused)
    handle = str(_launch(app, sid, root, "1")["handle"])
    _settled(app, handle)
    assert keys and not native_code.staged(keys[0])
