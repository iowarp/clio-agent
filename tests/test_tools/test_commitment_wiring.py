# pyright: reportPrivateUsage=false
"""tools/execution.py <-> runtime.commitment_activity wiring (#1230).

An unbounded ``wait_for_terminal`` commitment call (#1225: no explicit
caller-declared budget, the tool's own schema declares ``wait_for_terminal``)
must mark ``commitment_activity`` in flight for the SYNC executor's whole
foreground wait and clear it once the call returns — the signal
``gact/turn_watchdog.py`` reads to pause the turn's no-progress ceiling. A
call that resolves to a BOUNDED budget must never touch it at all.
"""

from __future__ import annotations

import threading
import time

from clio_agent.runtime import commitment_activity
from clio_agent.tools.execution import SyncMCPToolExecutor
from tests.test_tools.test_mutating_tool_timeouts import (  # type: ignore[attr-defined]
    _Client,
    _relay_jarvis_run_tool,
)


def setup_function() -> None:
    commitment_activity._INFLIGHT.clear()


def teardown_function() -> None:
    commitment_activity._INFLIGHT.clear()


def _wait_until(predicate, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_unbounded_commitment_call_marks_and_clears_commitment_activity() -> None:
    client = _Client(delay_seconds=0.2)
    executor = SyncMCPToolExecutor(
        object(),
        timeout=5.0,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    result_box: dict[str, str] = {}
    try:
        caller = threading.Thread(
            target=lambda: result_box.__setitem__(
                "result",
                executor.call_tool(
                    "relay_jarvis_run",
                    {"pipeline_id": "asteroid", "wait_for_terminal": True},
                ),
            ),
            daemon=True,
        )
        caller.start()
        assert _wait_until(commitment_activity.commitment_wait_in_flight), (
            "commitment_activity was never marked in flight for the unbounded call"
        )
        caller.join(timeout=5.0)
        assert not caller.is_alive()
    finally:
        executor.close()

    assert commitment_activity.commitment_wait_in_flight() is False
    assert '"pipeline_id": "asteroid"' in result_box["result"]


def test_bounded_call_never_touches_commitment_activity() -> None:
    """A call with an explicit caller-declared budget (finite) must never mark
    commitment_activity — only a genuine #1225 unbounded wait does."""

    client = _Client(delay_seconds=0.05)
    executor = SyncMCPToolExecutor(
        object(),
        timeout=5.0,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    try:
        executor.call_tool(
            "relay_jarvis_run",
            {
                "pipeline_id": "asteroid",
                "timeout_seconds": 5,
                "wait_for_terminal": True,
                "wait_timeout_seconds": 5,
            },
        )
    finally:
        executor.close()
    assert commitment_activity.commitment_wait_in_flight() is False
    assert commitment_activity._INFLIGHT == {}
