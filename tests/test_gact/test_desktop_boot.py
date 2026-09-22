"""Tests for the desktop-managed backend startup heartbeat."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import desktop_boot


class _ImmediateThread:
    """Thread stand-in that records startup without running the infinite loop."""

    starts = 0

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def start(self) -> None:
        """Record that the heartbeat thread would have started."""

        type(self).starts += 1


def test_heartbeat_is_disabled_outside_desktop(monkeypatch) -> None:
    """Ordinary CLI launches must not create a reporter thread."""

    monkeypatch.delenv("CLIO_DESKTOP_BOOT_HEARTBEAT", raising=False)
    monkeypatch.setattr(desktop_boot, "_heartbeat_started", False)
    monkeypatch.setattr(desktop_boot.threading, "Thread", _ImmediateThread)
    _ImmediateThread.starts = 0

    assert desktop_boot.start_desktop_boot_heartbeat() is False
    assert _ImmediateThread.starts == 0


def test_heartbeat_starts_once_for_legacy_and_module_entry_points(monkeypatch) -> None:
    """Both launch paths may request the reporter without duplicating it."""

    monkeypatch.setenv("CLIO_DESKTOP_BOOT_HEARTBEAT", "1")
    monkeypatch.setattr(desktop_boot, "_heartbeat_started", False)
    monkeypatch.setattr(desktop_boot.threading, "Thread", _ImmediateThread)
    _ImmediateThread.starts = 0

    assert desktop_boot.start_desktop_boot_heartbeat() is True
    assert desktop_boot.start_desktop_boot_heartbeat() is False
    assert _ImmediateThread.starts == 1
