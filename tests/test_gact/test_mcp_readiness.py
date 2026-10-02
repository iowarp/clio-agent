"""Bounded cold-MCP preparation coverage."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as runtime_context
from clio_agent.gact import mcp_readiness
from clio_agent.gact.events import EventBus


class _Executor:
    def __init__(self, *, fail_connect_once: bool = False, stall: bool = False) -> None:
        self.fail_connect_once = fail_connect_once
        self.stall = stall
        self.connect_attempts = 0
        self.merged: dict[str, Any] = {}

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self.merged.update(tools)

    def prepare_namespace(self, namespace: str) -> None:
        assert namespace == "geo"
        self.connect_attempts += 1
        if self.stall:
            from clio_agent.tools.mcp_server_progress import NoProgressTimeout

            raise NoProgressTimeout("connect", "no_progress", 30.0, 30.0)
        if self.fail_connect_once and self.connect_attempts == 1:
            raise ConnectionRefusedError("private endpoint detail")


def test_mount_retries_persistent_connect_with_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executor = _Executor(fail_connect_once=True)
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        lambda namespace, spec: {"geo_geocode": SimpleNamespace(name="geo_geocode")},
    )
    monkeypatch.setattr(mcp_readiness.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        mcp_readiness,
        "_publish_dependency_state",
        lambda namespace, **payload: events.append({"namespace": namespace, **payload}),
    )

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_readiness"):
        tools = mcp_readiness.mount_namespace_for_session(
            executor,
            "geo",
            SimpleNamespace(name="Geospatial tools"),
            retry_delays_s=(0.25,),
        )

    assert set(tools) == {"geo_geocode"}
    assert executor.connect_attempts == 2
    assert sleeps == [0.25]
    assert [(event["phase"], event["state"]) for event in events] == [
        ("launch", "running"),
        ("connect", "running"),
        ("retry", "retrying"),
        ("launch", "running"),
        ("connect", "running"),
        ("connect", "ready"),
    ]
    assert events[2]["reason"] == "mcp_namespace_discovery_unreachable"
    assert events[2]["retry_in_ms"] == 250
    assert events[-1]["tool_count"] == 1

    retries = [
        record
        for record in caplog.records
        if mcp_readiness.MCP_MOUNT_RETRY_REASON in record.getMessage()
    ]
    assert len(retries) == 1
    message = retries[0].getMessage()
    assert "namespace=geo" in message
    assert "phase=connect" in message
    assert "attempt=1/2" in message
    assert "retry_in_ms=250" in message
    # The raw subprocess/endpoint detail never rides the readiness reason.
    assert "private endpoint detail" not in message


def test_terminal_launcher_failure_raises_without_retrying(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executor = _Executor()
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_readiness,
        "_publish_dependency_state",
        lambda namespace, **payload: events.append({"namespace": namespace, **payload}),
    )

    def _missing_launcher(namespace: str, spec: Any) -> dict[str, Any]:
        del namespace, spec
        raise FileNotFoundError("private path")

    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        _missing_launcher,
    )

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_readiness"):
        with pytest.raises(FileNotFoundError):
            mcp_readiness.mount_namespace_for_session(
                executor,
                "geo",
                SimpleNamespace(name="Geospatial tools"),
                retry_delays_s=(0.25, 1.0),
            )

    # A non-retryable launcher failure is raised on attempt 1, never retried,
    # and is reported by the caller's typed mount_failures map -- not here.
    assert [
        record
        for record in caplog.records
        if mcp_readiness.MCP_MOUNT_RETRY_REASON in record.getMessage()
    ] == []
    assert [(event["phase"], event["state"]) for event in events] == [
        ("launch", "running"),
        ("launch", "failed"),
    ]
    assert events[-1]["reason"] == "mcp_namespace_discovery_unreachable"


def test_a_connect_that_stopped_progressing_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connect already waited out its no-progress window (the server's own tree did
    nothing); restarting it would only repeat that. Typed, terminal, one attempt.

    **Sabotage:** treat ``NoProgressTimeout`` as retryable -> 3 connect attempts.
    """
    from clio_agent.tools.mcp_server_progress import NoProgressTimeout

    executor = _Executor(stall=True)
    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        lambda namespace, spec: {"geo_geocode": SimpleNamespace(name="geo_geocode")},
    )
    monkeypatch.setattr(mcp_readiness, "_publish_dependency_state", lambda *a, **k: None)
    with pytest.raises(NoProgressTimeout):
        mcp_readiness.mount_namespace_for_session(
            executor, "geo", SimpleNamespace(name="g"), retry_delays_s=(0.0, 0.0)
        )
    assert executor.connect_attempts == 1


def test_namespace_title_preserves_known_acronyms() -> None:
    assert mcp_readiness._namespace_title("geo") == "Geo MCP"
    assert mcp_readiness._namespace_title("ndp") == "NDP MCP"
    assert mcp_readiness._namespace_title("pandas") == "Pandas MCP"


def test_dependency_state_publishes_the_v3_consumed_payload() -> None:
    bus = EventBus()
    app = SimpleNamespace(state=SimpleNamespace(bus=bus))
    app_token = runtime_context.set_app(app)
    session_token = runtime_context.set_session_id("sess_1")
    try:
        mcp_readiness._publish_dependency_state(
            "geo",
            phase="launch",
            state="running",
            attempt=1,
            max_attempts=3,
        )
    finally:
        runtime_context.reset(session_token)
        runtime_context.reset(app_token)

    events = bus.session_events_since("sess_1")
    assert len(events) == 1
    assert events[0].type == "infrastructure.dependency.changed"
    assert events[0].payload == {
        "id": "sess_1:mcp:geo",
        "session_id": "sess_1",
        "category": "mcp",
        "namespace": "geo",
        "title": "Geo MCP",
        "phase": "launch",
        "state": "running",
        "attempt": 1,
        "max_attempts": 3,
    }
