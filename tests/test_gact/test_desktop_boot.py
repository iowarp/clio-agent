"""Tests for the desktop-managed backend startup heartbeat."""

from __future__ import annotations

from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError

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


class _InlineThread:
    """Thread stand-in that runs the reporter loop on the calling thread."""

    def __init__(self, *, target: Any, **_kwargs: Any) -> None:
        self._target = target

    def start(self) -> None:
        """Run the reporter to completion."""

        self._target()


def _run_reporter(monkeypatch, capsys, readiness: list[bool]) -> list[str]:
    monkeypatch.setenv("CLIO_DESKTOP_BOOT_HEARTBEAT", "1")
    monkeypatch.setattr(desktop_boot, "_heartbeat_started", False)
    monkeypatch.setattr(desktop_boot, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(desktop_boot.threading, "Thread", _InlineThread)
    answers = iter(readiness)
    monkeypatch.setattr(desktop_boot, "_capabilities_ready", lambda: next(answers))

    assert desktop_boot.start_desktop_boot_heartbeat() is True
    return [line for line in capsys.readouterr().err.splitlines() if line]


def test_heartbeat_reports_only_while_the_backend_is_not_ready(monkeypatch, capsys) -> None:
    """No "active" line may follow readiness, or the desktop's progress goes backwards."""

    lines = _run_reporter(monkeypatch, capsys, [False, False, True])

    assert lines == ["sidecar-progress: backend startup is active"] * 2


def test_heartbeat_is_silent_when_the_backend_is_already_ready(monkeypatch, capsys) -> None:
    assert _run_reporter(monkeypatch, capsys, [True]) == []


def _http_error(code: int) -> HTTPError:
    return HTTPError("http://127.0.0.1/v1/capabilities", code, "status", Message(), None)


def test_capabilities_probe_treats_only_the_startup_gate_as_starting(monkeypatch) -> None:
    """A 401 means the server answers; probing must not keep the heartbeat alive forever."""

    monkeypatch.setattr(desktop_boot.sys, "argv", ["clio-agent", "--port", "1"])
    for code, ready in ((503, False), (401, True), (404, True)):

        def _raise(*_args: Any, _code: int = code, **_kwargs: Any) -> None:
            raise _http_error(_code)

        monkeypatch.setattr(desktop_boot, "urlopen", _raise)
        assert desktop_boot._capabilities_ready() is ready, code

    def _refused(*_args: Any, **_kwargs: Any) -> None:
        raise URLError(ConnectionRefusedError())

    monkeypatch.setattr(desktop_boot, "urlopen", _refused)
    assert desktop_boot._capabilities_ready() is False
