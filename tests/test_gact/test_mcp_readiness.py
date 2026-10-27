"""Bounded cold-MCP preparation coverage."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as runtime_context
from clio_agent.gact import mcp_readiness
from clio_agent.gact.events import EventBus


class _Tool:
    """A listed MCP tool: a name and the rename a mount applies."""

    def __init__(self, name: str) -> None:
        self.name = name

    def model_copy(self, *, update: dict[str, Any]) -> "_Tool":
        return _Tool(update["name"])


class _Executor:
    """The executor surface a preparation uses: one start lists over its connection."""

    def __init__(
        self, *, fail_start_once: bool = False, stall: bool = False, listed: tuple[str, ...] = ()
    ) -> None:
        self.fail_start_once = fail_start_once
        self.stall = stall
        self.start_attempts = 0
        self.connects = 0
        self.merged: dict[str, Any] = {name: _Tool(name) for name in listed}

    def get_tool_names(self) -> list[str]:
        return list(self.merged)

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self.merged.update(tools)

    def list_namespace_tools(self, namespace: str) -> list[Any]:
        assert namespace == "geo"
        self.start_attempts += 1
        if self.stall:
            from clio_agent.tools.mcp_server_progress import NoProgressTimeout

            raise NoProgressTimeout("connect", "no_progress", 30.0, 30.0)
        if self.fail_start_once and self.start_attempts == 1:
            raise ConnectionRefusedError("private endpoint detail")
        return [_Tool("geocode")]

    def prepare_namespace(self, namespace: str) -> None:
        assert namespace == "geo"
        self.connects += 1


@pytest.fixture(autouse=True)
def _no_provisioning(monkeypatch: pytest.MonkeyPatch) -> Any:
    """These servers have nothing to provision; their preparation state is reset."""

    from clio_agent.tools import mcp_prepare

    monkeypatch.setattr(
        mcp_prepare, "provision", lambda namespace, spec: mcp_prepare.PROVISION_NOT_APPLICABLE
    )
    yield
    mcp_prepare.forget("geo")


def test_mount_retries_a_failed_start_with_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from clio_agent.tools import mcp_prepare

    executor = _Executor(fail_start_once=True)
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []
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
    assert set(executor.merged) == {"geo_geocode"}
    # One start per attempt: the listing rides the connection, no second spawn.
    assert executor.start_attempts == 2
    assert executor.connects == 0
    assert sleeps == [0.25]
    assert [(event["phase"], event["state"]) for event in events] == [
        ("provision", "running"),
        ("launch", "running"),
        ("retry", "retrying"),
        ("provision", "running"),
        ("launch", "running"),
        ("launch", "ready"),
    ]
    assert events[2]["reason"] == mcp_prepare.START_FAILED
    assert events[2]["retry_in_ms"] == 250
    assert events[-1]["tool_count"] == 1
    state = mcp_prepare.preparation_state("geo")
    assert state is not None
    assert state["state"] == mcp_prepare.READY
    assert state["provision"] == mcp_prepare.PROVISION_NOT_APPLICABLE

    retries = [
        record
        for record in caplog.records
        if mcp_readiness.MCP_MOUNT_RETRY_REASON in record.getMessage()
    ]
    assert len(retries) == 1
    message = retries[0].getMessage()
    assert "namespace=geo" in message
    assert "phase=launch" in message
    assert "attempt=1/2" in message
    assert "retry_in_ms=250" in message
    # The raw subprocess/endpoint detail never rides the readiness reason.
    assert "private endpoint detail" not in message


def test_a_listed_namespace_only_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached listing needs no listing: the one start is the connection."""

    executor = _Executor(listed=("geo_geocode",))
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_readiness,
        "_publish_dependency_state",
        lambda namespace, **payload: events.append({"namespace": namespace, **payload}),
    )

    tools = mcp_readiness.mount_namespace_for_session(
        executor, "geo", SimpleNamespace(name="g"), retry_delays_s=()
    )

    assert set(tools) == {"geo_geocode"}
    assert (executor.connects, executor.start_attempts) == (1, 0)
    assert [(event["phase"], event["state"]) for event in events] == [
        ("provision", "running"),
        ("connect", "running"),
        ("connect", "ready"),
    ]


def test_a_provision_failure_is_a_typed_terminal_failed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.tools import mcp_prepare

    executor = _Executor()
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_readiness,
        "_publish_dependency_state",
        lambda namespace, **payload: events.append({"namespace": namespace, **payload}),
    )

    def _broken(namespace: str, spec: Any) -> str:
        raise mcp_prepare.PrepareError(mcp_prepare.PROVISION_FAILED, "uv sync exited 2")

    monkeypatch.setattr(mcp_prepare, "provision", _broken)

    with pytest.raises(mcp_prepare.PrepareError):
        mcp_readiness.mount_namespace_for_session(
            executor, "geo", SimpleNamespace(name="g"), retry_delays_s=(0.0, 0.0)
        )

    # Not retried (a broken environment does not heal in a second), never started.
    assert executor.start_attempts == 0
    assert [(event["phase"], event["state"]) for event in events] == [
        ("provision", "running"),
        ("provision", "failed"),
    ]
    state = mcp_prepare.preparation_state("geo")
    assert state is not None
    assert (state["state"], state["phase"], state["reason"]) == (
        mcp_prepare.FAILED,
        mcp_prepare.PHASE_PROVISION,
        mcp_prepare.PROVISION_FAILED,
    )
    assert state["detail"] == "uv sync exited 2"


def test_terminal_launcher_failure_of_a_listing_only_mount_raises_without_retrying(
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
                connect=False,
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


def test_a_start_that_stopped_progressing_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start already waited out its no-progress window (the server's own tree did
    nothing); restarting it would only repeat that. Typed, terminal, one attempt.

    **Sabotage:** treat ``NoProgressTimeout`` as retryable -> 3 start attempts.
    """
    from clio_agent.tools import mcp_prepare
    from clio_agent.tools.mcp_server_progress import NoProgressTimeout

    executor = _Executor(stall=True)
    monkeypatch.setattr(mcp_readiness, "_publish_dependency_state", lambda *a, **k: None)
    with pytest.raises(NoProgressTimeout):
        mcp_readiness.mount_namespace_for_session(
            executor, "geo", SimpleNamespace(name="g"), retry_delays_s=(0.0, 0.0)
        )
    assert executor.start_attempts == 1
    state = mcp_prepare.preparation_state("geo")
    assert state is not None
    assert state["reason"] == mcp_prepare.START_NO_PROGRESS


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
