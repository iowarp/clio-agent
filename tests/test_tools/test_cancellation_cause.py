"""A cancelled tool call names who cancelled it: the person, or the service stopping.

The model reads the cancelled call's error. A call the agent service's shutdown stopped
used to read "tool call cancelled by client", and the model then told the person they
had cancelled it (continuity grid CC-O2). The live turn's cancellation checker carries
the cause (``checker.cause``); a call the person stopped still reads as theirs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.gact.cancellation_check import make_cancellation_checker
from clio_agent.gact.tool_observer_parts import completed_tool_outcome
from clio_agent.tools import foreground_cancellation as foreground_cancel
from clio_agent.tools.execution import (
    ToolRuntimeHooks,
    set_tool_runtime_fallback,
    set_tool_runtime_resolver,
)


@pytest.fixture
def hooks():
    """Install a tool-runtime bundle whose checker reports ``cause``."""

    def install(cause: str | None) -> None:
        def check() -> bool:
            return True

        if cause is not None:
            check.cause = lambda: cause  # type: ignore[attr-defined]
        set_tool_runtime_resolver(None)
        set_tool_runtime_fallback(ToolRuntimeHooks(cancellation_checker=check))

    yield install
    set_tool_runtime_fallback(ToolRuntimeHooks())


@pytest.mark.parametrize("cause", [None, ""], ids=["no-cause", "person"])
def test_a_call_the_person_stopped_still_reads_as_theirs(hooks, cause: str | None) -> None:
    hooks(cause)
    error = foreground_cancel._tool_cancellation_error("shell_bash", "tool_call_in_flight")
    assert str(error) == "tool call cancelled by client"
    assert "cause" not in error.details
    waiting = foreground_cancel._tool_cancellation_error(
        "fs_write_file", "permission_wait", before_approval="'fs_write_file' did not run"
    )
    assert str(waiting) == (
        "tool call cancelled by client before it was approved: 'fs_write_file' did not run"
    )


def test_a_call_the_shutdown_stopped_says_so(hooks) -> None:
    """SABOTAGE: return "" from ``cancellation_cause`` and both read "cancelled by client"."""
    hooks("server_shutdown")
    error = foreground_cancel._tool_cancellation_error("shell_bash", "tool_call_in_flight")
    assert str(error) == "tool call stopped because the agent service is shutting down"
    assert "client" not in str(error)
    assert error.details["cause"] == "server_shutdown"
    waiting = foreground_cancel._tool_cancellation_error(
        "fs_write_file", "permission_wait", before_approval="'fs_write_file' did not run"
    )
    assert str(waiting) == (
        "tool call stopped before it was approved because the agent service is shutting "
        "down: 'fs_write_file' did not run"
    )
    # The transcript still records it as a cancelled call.
    assert completed_tool_outcome(str(error), None, refused=False) == "cancelled"


@pytest.mark.parametrize(("should_exit", "expected"), [(True, "server_shutdown"), (False, "")])
def test_the_gact_checker_reports_the_shutdown_as_the_cause(
    should_exit: bool, expected: str
) -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            uvicorn_server=SimpleNamespace(should_exit=should_exit),
            turn_runner=SimpleNamespace(closing=False),
            cancel_events={},
            cancel_flags=set(),
        )
    )
    check = make_cancellation_checker(app)  # type: ignore[arg-type]
    assert check.cause() == expected  # type: ignore[attr-defined]
