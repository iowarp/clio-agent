"""Session-visible MCP preparation lifecycle coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import mcp_readiness


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


class _Executor:
    def __init__(self, *, fail_connect_once: bool = False) -> None:
        self.fail_connect_once = fail_connect_once
        self.connect_attempts = 0
        self.connect_timeouts: list[float | None] = []
        self.merged: dict[str, Any] = {}
        self._setup_timeout = 10.0

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self.merged.update(tools)

    def prepare_namespace(self, namespace: str, *, timeout: float | None = None) -> None:
        assert namespace == "geo"
        self.connect_attempts += 1
        self.connect_timeouts.append(timeout)
        if self.fail_connect_once and self.connect_attempts == 1:
            raise ConnectionRefusedError("private endpoint detail")


def _capture_bus(monkeypatch: pytest.MonkeyPatch) -> _Bus:
    bus = _Bus()
    app = SimpleNamespace(state=SimpleNamespace(bus=bus))
    monkeypatch.setattr(mcp_readiness._ctx, "active_app", lambda: app)
    monkeypatch.setattr(mcp_readiness._ctx, "active_session_id", lambda: "sess_demo")
    monkeypatch.setattr(mcp_readiness._ctx, "active_tool_session_id", lambda: "")
    return bus


def test_mount_retries_persistent_connect_and_emits_causal_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _capture_bus(monkeypatch)
    executor = _Executor(fail_connect_once=True)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        lambda namespace, spec: {"geo_geocode": SimpleNamespace(name="geo_geocode")},
    )
    monkeypatch.setattr(mcp_readiness.time, "sleep", sleeps.append)

    tools = mcp_readiness.mount_namespace_for_session(
        executor,
        "geo",
        SimpleNamespace(name="Geospatial tools"),
        retry_delays_s=(0.25,),
    )

    assert set(tools) == {"geo_geocode"}
    assert executor.connect_attempts == 2
    assert executor.connect_timeouts == [10.0, 30.0]
    assert sleeps == [0.25]
    payloads = [event.payload for event in bus.events]
    assert [(row["phase"], row["state"]) for row in payloads] == [
        ("launch", "running"),
        ("connect", "running"),
        ("connect", "retrying"),
        ("launch", "running"),
        ("connect", "running"),
        ("connect", "ready"),
    ]
    assert payloads[-1]["tool_count"] == 1
    assert all("private endpoint detail" not in str(row) for row in payloads)


def test_terminal_launcher_failure_is_visible_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _capture_bus(monkeypatch)
    executor = _Executor()

    def _missing_launcher(namespace: str, spec: Any) -> dict[str, Any]:
        del namespace, spec
        raise FileNotFoundError("private path")

    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        _missing_launcher,
    )

    with pytest.raises(FileNotFoundError):
        mcp_readiness.mount_namespace_for_session(
            executor,
            "geo",
            SimpleNamespace(name="Geospatial tools"),
            retry_delays_s=(0.25, 1.0),
        )

    payloads = [event.payload for event in bus.events]
    assert [(row["phase"], row["state"]) for row in payloads] == [
        ("launch", "running"),
        ("launch", "failed"),
    ]
    assert all("private path" not in str(row) for row in payloads)
