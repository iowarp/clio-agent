"""A serving CLIO stops within one budget, whatever is still running (#1401)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from clio_agent.gact import shutdown_budget


def _app(*, server: bool = True) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(server_boot=True, owns_process=server))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 20.0), ("7.5", 7.5), ("0", 20.0), ("-3", 20.0), ("soon", 20.0)],
)
def test_the_budget_comes_from_the_setting_or_the_default(raw: str, expected: float) -> None:
    assert shutdown_budget.budget_seconds({"CLIO_SHUTDOWN_BUDGET_S": raw}) == expected


def test_the_default_sits_under_the_supervisors_force_window() -> None:
    # The desktop supervisor, the sidecar launcher and `clio stop` force a server at 30 s.
    assert shutdown_budget.DEFAULT_BUDGET_S < 30


def test_an_embedded_or_test_app_is_never_bounded() -> None:
    assert shutdown_budget.begin(_app(server=False)) is None


def test_a_stop_past_its_budget_ends_the_process_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_SHUTDOWN_BUDGET_S", "0.05")
    monkeypatch.setattr(shutdown_budget.logging, "shutdown", lambda: None)
    ended = threading.Event()
    codes: list[int] = []

    def exit_process(code: int) -> None:
        codes.append(code)
        ended.set()

    app = _app()
    first = shutdown_budget.begin(app, exit_process=exit_process)
    assert shutdown_budget.begin(app, exit_process=exit_process) is first  # armed once
    assert ended.wait(5)
    assert codes == [shutdown_budget.EXIT_BUDGET_EXCEEDED]


def test_a_finished_stop_disarms_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_SHUTDOWN_BUDGET_S", "0.2")
    codes: list[int] = []
    done = shutdown_budget.begin(_app(), exit_process=codes.append)
    assert done is not None
    done.set()
    time.sleep(0.4)
    assert codes == []


def test_a_hung_shutdown_still_ends_the_real_process_with_a_reason() -> None:
    """Hang after shutdown begins (a turn ignoring cancellation): the process still ends."""

    script = textwrap.dedent(
        """
        import logging, os, threading, time
        from types import SimpleNamespace
        from clio_agent.gact import shutdown_budget
        logging.basicConfig(level=logging.INFO)
        os.environ["CLIO_SHUTDOWN_BUDGET_S"] = "0.5"
        shutdown_budget.begin(SimpleNamespace(state=SimpleNamespace(owns_process=True)))
        threading.Event().wait()  # a non-daemon wait that never ends
        """
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == shutdown_budget.EXIT_BUDGET_EXCEEDED, result.stderr
    assert time.monotonic() - started < 15
    assert "reason=shutdown_budget_exceeded" in result.stderr
    assert "Thread" in result.stderr  # every thread's stack, to find what held the stop


def test_a_test_app_running_the_server_boot_is_never_bounded() -> None:
    """Tests run the server's boot steps in their own process: the watchdog must not
    arm there, or it ends the test worker 20 seconds after that test's shutdown."""

    app = SimpleNamespace(state=SimpleNamespace(server_boot=True))
    assert shutdown_budget.begin(app, exit_process=lambda code: None) is None


def test_run_server_marks_its_app_as_owning_the_process() -> None:
    from clio_agent.gact import server_boot

    app = SimpleNamespace(state=SimpleNamespace())
    server_boot.mark_real_server(app)  # type: ignore[arg-type]
    assert app.state.server_boot is True and app.state.owns_process is True
