"""Bounded cold-MCP preparation coverage.

The readiness ladder used to publish an ``infrastructure.dependency.changed``
bus event per attempt -- a vocabulary no client, schema or route consumed, and
one every generic bus reader filters out. The retry is now recorded the way
every other bounded retry in the tree is: a typed loud warning. The lane that
reaches a consumer is unchanged (a terminal failure raises into
``builders._resolve_requested_tools``, which records the typed
``mount_failures`` reason the ``_UnsupportedSessionAgent`` boundary carries).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import mcp_readiness


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


def test_mount_retries_persistent_connect_with_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executor = _Executor(fail_connect_once=True)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "clio_agent.tools.mcp_discovery.ensure_namespace",
        lambda namespace, spec: {"geo_geocode": SimpleNamespace(name="geo_geocode")},
    )
    monkeypatch.setattr(mcp_readiness.time, "sleep", sleeps.append)

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_readiness"):
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


def test_readiness_publishes_no_bus_event_vocabulary() -> None:
    """No dead wire vocabulary: the module owns no event type at all."""

    source = mcp_readiness.__file__
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "infrastructure.dependency" not in body
    assert "bus.publish" not in body
