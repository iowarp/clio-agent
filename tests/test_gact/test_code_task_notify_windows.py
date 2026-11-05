"""A background script's notify windows past the happy path (taskable CT9).

Queued notifies are never dropped: a clean shutdown sends them, a failed send keeps them
for the next attempt, and windows nobody uses are forgotten.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import code_task_reporting as reporting


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, list[str]]]]:
    """Fresh windows; ``_send`` records ``(owner, messages)``. A long interval."""
    monkeypatch.setattr(reporting, "_WINDOWS", {})
    monkeypatch.setenv("CLIO_CODE_NOTIFY_MIN_INTERVAL_S", "60")
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        reporting,
        "_send",
        lambda _app, owner, entries: sent.append((owner, [e["message"] for e in entries])),
    )
    yield sent
    for window in reporting._WINDOWS.values():
        if window.timer is not None:
            window.timer.cancel()


def _entry(message: str, handle: str = "task_a") -> dict[str, Any]:
    return {"handle": handle, "label": "watch", "message": message, "data": None, "at": "x"}


def test_a_clean_shutdown_sends_every_queued_notify(windows: Any) -> None:
    """SABOTAGE: return from ``flush_all`` without sending -> the queued notify is lost
    with the process -> red."""
    app, live = SimpleNamespace(), {}
    reporting._notify(app, "sess_owner", _entry("first"), live)
    reporting._notify(app, "sess_owner", _entry("second"), live)
    assert windows == [("sess_owner", ["first"])]
    assert reporting.flush_all(app) == 0
    assert windows == [("sess_owner", ["first"]), ("sess_owner", ["second"])]
    assert "sess_owner" not in reporting._WINDOWS  # its timer was cancelled with it


def test_a_failed_send_keeps_its_notifies_for_the_next_attempt(
    windows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the re-queue in ``_deliver`` -> the notify is gone -> red."""
    app, live = SimpleNamespace(), {}
    recorded = reporting._send
    calls = [0]

    def flaky(app_: Any, owner: str, entries: list[dict[str, Any]]) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("history unavailable")
        recorded(app_, owner, entries)

    monkeypatch.setattr(reporting, "_send", flaky)
    assert reporting._notify(app, "sess_owner", _entry("alarm"), live) == {
        "delivered": False,
        "queued": True,
    }
    window = reporting._WINDOWS["sess_owner"]
    assert [e["message"] for e in window.pending] == ["alarm"] and window.timer is not None
    reporting._flush(app, "sess_owner")  # the window's end
    assert windows == [("sess_owner", ["alarm"])]


def test_a_settling_task_sends_only_its_own_queued_notifies(windows: Any) -> None:
    app, live = SimpleNamespace(), {}
    reporting._notify(app, "sess_owner", _entry("a1"), live)
    reporting._notify(app, "sess_owner", _entry("a2"), live)
    reporting._notify(app, "sess_owner", _entry("b1", handle="task_b"), live)
    assert reporting.flush_task(app, "sess_owner", "task_a") == 1
    assert windows[-1] == ("sess_owner", ["a2"])
    window = reporting._WINDOWS["sess_owner"]
    assert [e["message"] for e in window.pending] == ["b1"] and window.timer is not None


def test_windows_nobody_used_for_an_interval_are_forgotten(
    windows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: skip ``_prune`` -> one window per conversation that ever notified,
    forever -> red."""
    monkeypatch.setenv("CLIO_CODE_NOTIFY_MIN_INTERVAL_S", "0")
    app = SimpleNamespace()
    for owner in ("sess_1", "sess_2", "sess_3"):
        reporting._notify(app, owner, _entry("hi"), {})
    assert len(reporting._WINDOWS) == 3
    reporting.flush_task(app, "sess_1", "task_a")  # a task settles
    assert reporting._WINDOWS == {}
