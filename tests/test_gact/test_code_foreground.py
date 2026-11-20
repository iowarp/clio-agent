"""A running foreground script moves to a background task, by time or by the person.

The REAL app, task layer and kernels: ``run_script`` runs the script on its own thread;
past ``code.foreground_background_after_s`` (or on ``request_background``) the running
kernel becomes a background task's, the call returns the task's handle, and the scope's
next script starts a new kernel. Cancelling the task stops the moved script.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.code_mode import foreground, kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.task_access import TaskCaller
from clio_agent.tools.execution import tool_workspace_context
from tests.test_gact.task_records import active_turn_fixture
from tests.test_gact.test_code_mode import _agent
from tests.test_gact.test_native_code import _settled, world  # noqa: F401 - fixture

pytestmark = [pytest.mark.usefixtures("host_agent_executor"), pytest.mark.timeout(120)]


def _run(app: Any, sid: str, root: Path, code: str) -> dict[str, Any]:
    with active_turn_fixture(app, sid), tool_workspace_context(str(root)):
        return tool_module.run_script(_agent(), code, session_id=sid, scope="main", app=app)


def test_a_long_script_moves_to_the_background_with_its_state(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the time, the call returns a task handle; the task finishes the script with
    its variables; the scope's next script has a new kernel and says why.
    SABOTAGE: skip ``kernels.detach`` and the next script finds ``x`` (same kernel)."""
    app, sid, root = world
    monkeypatch.setattr(foreground, "background_after_s", lambda: 0.5)
    code = "x = 41\nimport asyncio\nawait asyncio.sleep(3)\nprint('done', x + 1)"
    outcome = _run(app, sid, root, code)
    assert outcome["status"] == foreground.BACKGROUNDED and outcome["moved_by"] == "time"
    assert outcome["handle"].startswith("task_") and "ran past 0.5 s" in outcome["text"]
    assert (sid, "main") not in kernels._KERNELS  # the running kernel left the scope
    task = _settled(app, outcome["handle"])
    assert task.kind == "Code" and task.status == "completed", task
    monkeypatch.setattr(foreground, "background_after_s", lambda: 0.0)
    after = _run(app, sid, root, "x")
    assert after["status"] == "failed" and "NameError" in after["error"]
    assert "went with a script moved to the background" in tool_module.model_text(after)


def test_the_person_sends_a_running_script_to_the_background(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session's running script is listed; asking moves it, and the call returns.
    SABOTAGE: ignore ``move`` in ``wait`` and the call blocks until the script ends."""
    app, sid, root = world
    monkeypatch.setattr(foreground, "background_after_s", lambda: 0.0)  # never by time
    result: dict[str, Any] = {}
    caller = threading.Thread(
        target=lambda: result.update(
            _run(app, sid, root, "import asyncio\nawait asyncio.sleep(600)")
        )
    )
    caller.start()
    deadline = time.monotonic() + 30
    while not foreground.running_scripts(sid) and time.monotonic() < deadline:
        time.sleep(0.05)
    [listed] = foreground.running_scripts(sid)
    assert listed["label"] == "import asyncio"
    assert foreground.request_background(sid) == 1
    caller.join(timeout=30)
    assert not caller.is_alive()
    assert result["status"] == foreground.BACKGROUNDED and result["moved_by"] == "person"
    assert "the person asked" in result["text"]
    assert foreground.running_scripts(sid) == []  # now a task, not a foreground script
    _cancel(app, sid, result["handle"])


def _cancel(app: Any, sid: str, handle: str) -> Any:
    cancel = app.state.task_operations.cancel(TaskCaller(sid, app.state.human_principal), handle)
    boundary = app.state.builtin_boundary
    asyncio.run_coroutine_threadsafe(cancel, boundary._loop).result(timeout=30)
    return _settled(app, handle)


def test_cancelling_a_moved_script_stops_its_kernel(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The moved script's cancel is its task's: cancelling stops the kernel it took.
    SABOTAGE: drop the stop hand-over in ``adopt`` and the task never settles."""
    app, sid, root = world
    monkeypatch.setattr(foreground, "background_after_s", lambda: 0.3)
    outcome = _run(app, sid, root, "import asyncio\nawait asyncio.sleep(600)")
    kernel_count = sum(1 for key in kernels._KERNELS if key[0] == sid)
    task = _cancel(app, sid, outcome["handle"])
    assert task.status == "cancelled", task
    assert sum(1 for key in kernels._KERNELS if key[0] == sid) <= kernel_count


def test_a_short_script_stays_in_the_foreground(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sid, root = world
    monkeypatch.setattr(foreground, "background_after_s", lambda: 30.0)
    outcome = _run(app, sid, root, "y = 2\ny * 21")
    assert outcome["status"] == "completed" and outcome["value"] in (42, "42")
    assert (sid, "main") in kernels._KERNELS


def test_a_moved_script_presents_where_it_went_and_why() -> None:
    """The row says it moved; expansion says when and why, and links the task. A long
    label and script stay whole. SABOTAGE: drop the ``moved`` branch and the row reads
    as a finished script with no output."""
    from clio_agent.gact.code_mode.presentation import present

    code = "\n".join(f"tile_{i} = await read_window('very long tile name {i}')" for i in range(40))
    row = {
        "status": foreground.BACKGROUNDED,
        "handle": "task_" + "a" * 32,
        "code": code,
        "calls": 1234,
        "elapsed_s": 181.4,
        "moved_by": "time",
        "moved": "Still running after 181.4 s (1234 tool calls so far); moved to the "
        "background because it ran past 180 s.",
    }
    view = present({"code": code}, "text for the model", row)
    flat = repr(view)
    assert "Moved to the background" in flat and "181.4 s" in flat
    assert "Show the task in Work" in flat and row["handle"] in flat
    assert "tile_39" in flat  # the whole script is kept for the reader
