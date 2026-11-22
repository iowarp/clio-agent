"""A background script's repeated calls are one counted record per window (CT5).

Through the real observer choke point (``notify_tool_observer``) with a recording observer:
what reaches the observer is what clio-core would store.
"""

from __future__ import annotations

import time
from typing import Any

from clio_agent.gact.code_task_records import CallGrouper
from clio_agent.tools.tool_observation import OBSERVATION_GROUP, notify_tool_observer


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str, str | None, Any]] = []

    def __call__(
        self, name: str, args: dict[str, Any], phase: str, error: str | None, result: Any = None
    ) -> Any:
        self.calls.append((name, args, phase, error, result))
        return "handle"


def _call(observer: Recorder, name: str, args: dict[str, Any], error: str | None = None) -> None:
    notify_tool_observer(observer, name, args, "started", None)
    notify_tool_observer(observer, name, args, "completed", error, {"ok": args})


def _grouped(grouper: CallGrouper, body: Any) -> None:
    token = OBSERVATION_GROUP.set(grouper)
    try:
        body()
    finally:
        OBSERVATION_GROUP.reset(token)


def test_repeated_successful_calls_are_one_counted_record() -> None:
    """SABOTAGE: let every call through -> ten notifications instead of two -> red."""
    observer, grouper = Recorder(), CallGrouper(window_s=60)

    def body() -> None:
        for i in range(5):
            _call(observer, "update_a2ui_data_model", {"value": i})

    _grouped(grouper, body)
    assert observer.calls == []  # held, nothing written yet
    grouper.flush()
    assert [c[2] for c in observer.calls] == ["started", "completed"]
    name, args, _phase, error, result = observer.calls[1]
    assert name == "update_a2ui_data_model" and error is None
    assert args["value"] == 4 and result == {"ok": {"value": 4}}
    assert args["_clio_repeat"]["count"] == 5


def test_an_error_is_recorded_at_once_and_ends_the_held_run() -> None:
    observer, grouper = Recorder(), CallGrouper(window_s=60)

    def body() -> None:
        _call(observer, "fetch", {"n": 1})
        _call(observer, "fetch", {"n": 2})
        _call(observer, "fetch", {"n": 3}, error="ConnectionError: refused")

    _grouped(grouper, body)
    phases = [(c[2], c[1].get("_clio_repeat", {}).get("count"), c[3]) for c in observer.calls]
    assert phases == [
        ("started", 2, None),
        ("completed", 2, None),
        ("started", None, None),
        ("completed", None, "ConnectionError: refused"),
    ]


def test_a_call_that_asked_the_person_is_recorded_on_its_own() -> None:
    asked = [0]
    observer, grouper = Recorder(), CallGrouper(asked=lambda: asked[0], window_s=60)

    def body() -> None:
        notify_tool_observer(observer, "write", {"p": "a"}, "started", None)
        asked[0] += 1  # the gate asked the person during this call
        notify_tool_observer(observer, "write", {"p": "a"}, "completed", None, "done")

    _grouped(grouper, body)
    assert [(c[2], "_clio_repeat" in c[1]) for c in observer.calls] == [
        ("started", False),
        ("completed", False),
    ]


def test_a_passed_window_records_the_held_run_and_starts_another() -> None:
    observer, grouper = Recorder(), CallGrouper(window_s=0.2)

    def body() -> None:
        _call(observer, "poll", {"i": 1})
        _call(observer, "poll", {"i": 2})
        time.sleep(0.3)
        _call(observer, "poll", {"i": 3})

    _grouped(grouper, body)
    assert [c[1]["_clio_repeat"]["count"] for c in observer.calls] == [2, 2]
    grouper.flush()
    assert observer.calls[-1][1]["_clio_repeat"]["count"] == 1


def test_outside_a_background_script_every_call_is_recorded() -> None:
    observer = Recorder()
    for i in range(3):
        _call(observer, "fetch", {"n": i})
    assert len(observer.calls) == 6


def test_a_running_calls_progress_message_reaches_the_task_row() -> None:
    """A nested call has no row while it runs, so its stall notice goes to the task's.

    SABOTAGE: drop the grouper's progress notice -> the stall stays log-only -> red.
    """
    notices: list[tuple[str, str | None]] = []
    observer = Recorder()
    grouper = CallGrouper(window_s=60, notice=lambda tool, text: notices.append((tool, text)))
    stall = "No reply from relay exec_read for 30s; still waiting. Stop the turn to cancel."

    def body() -> None:
        notify_tool_observer(observer, "relay_exec_read", {}, "started", None)
        for message in (stall, '{"type": "clio.terminal.chunk", "text": "x"}', "  "):
            progress = {"observer_handle": None, "progress": 30.0, "message": message}
            notify_tool_observer(observer, "relay_exec_read", {}, "progress", None, progress)
        notify_tool_observer(observer, "relay_exec_read", {}, "completed", None, {"ok": 1})

    _grouped(grouper, body)
    assert notices == [("relay_exec_read", stall), ("relay_exec_read", None)]
    assert observer.calls == []  # the call itself is still held for the grouped record


def test_the_task_row_shows_a_nested_calls_notice_until_it_ends() -> None:
    from clio_agent.gact import native_code
    from clio_agent.gact.external_call_stall import STOP_HINT, TASK_STOP_HINT

    live: dict[str, Any] = {"started": time.monotonic()}
    notice = native_code._call_notice(live)
    notice("live-move_move_wait", f"No reply from live-move move_wait for 30s. {STOP_HINT}")
    row = native_code.status(live, live["started"])
    assert row["call_notice"].endswith(TASK_STOP_HINT)  # a task outlives the turn
    assert "No reply from live-move move_wait for 30s" in row["summary"]
    notice("live-move_move_wait", None)
    assert "call_notice" not in native_code.status(live, live["started"])
