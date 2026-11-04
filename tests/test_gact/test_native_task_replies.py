"""A shell task polls fast, and a reply that changes nothing is not recorded.

L1P and L2 (2026-11-04): a shell command was seen as done a median 375 ms after it
ended (p90 1.36 s), because the task advertised fastmcp's 5 s poll ceiling and the
client's backoff climbed toward it. Every poll reply was a recorded snapshot, so
polling faster alone would have multiplied writes on long commands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.native_task_replies import REFRESH_S, unchanged_reply


def _snap(**fields: Any) -> Any:
    base = {
        "terminal_at": None,
        "raw_status": "working",
        "status": "running",
        "activity": "executing",
        "progress": None,
        "connection_freshness": "fresh",
        "updated_at": "2026-11-04T07:00:00+00:00",
    }
    return SimpleNamespace(**{**base, **fields})


def _at(seconds: float) -> str:
    start = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
    return (start + timedelta(seconds=seconds)).isoformat()


def test_a_repeated_running_reply_is_not_recorded_until_the_refresh() -> None:
    """SABOTAGE: return False always and every poll is a write again."""
    current = _snap()
    assert unchanged_reply(current, _snap(updated_at=_at(0.4)), _at(0.4))
    assert not unchanged_reply(current, _snap(updated_at=_at(REFRESH_S)), _at(REFRESH_S))


@pytest.mark.parametrize(
    "change",
    [
        {"raw_status": "completed"},
        {"status": "input_required"},
        {"activity": "cancelling"},
        {"progress": "50%"},
        {"connection_freshness": "lost"},
        {"terminal_at": "2026-11-04T07:00:01+00:00"},
    ],
)
def test_any_change_or_an_end_is_recorded(change: dict[str, Any]) -> None:
    """SABOTAGE: compare only the raw status and a progress message is lost."""
    assert not unchanged_reply(_snap(), _snap(**change), _at(0.4))


def test_the_shell_task_advertises_the_configured_poll_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: register the tool with TaskConfig's default and the ceiling is 5 s."""
    from clio_agent.gact import native_shell

    assert native_shell.shell_poll_interval() == timedelta(milliseconds=200)
    monkeypatch.setenv("CLIO_TOOLS_SHELL_POLL_INTERVAL_MS", "5")
    assert native_shell.shell_poll_interval() == timedelta(milliseconds=20)  # floor
    registered: dict[str, Any] = {}

    class _Root:
        def tool(self, **kwargs: Any) -> Any:
            registered.update(kwargs)
            return lambda fn: fn

    monkeypatch.setenv("CLIO_TOOLS_SHELL_POLL_INTERVAL_MS", "150")
    native_shell.register_native_shell(_Root(), object())  # type: ignore[arg-type]
    assert registered["task"].poll_interval == timedelta(milliseconds=150)
