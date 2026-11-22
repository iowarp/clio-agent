"""Single-flight preparation and the first-request tool-definition barrier."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from clio_agent.gact import mcp_background_prepare as background
from clio_agent.tools import mcp_prepare

_BACKSTOP_S = 30.0


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    for namespace in ("geo", "ndp"):
        mcp_prepare.forget(namespace)


def test_concurrent_callers_join_one_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    starts: list[str] = []

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        starts.append(namespace)
        release.wait(_BACKSTOP_S)
        return {"geo_x": object()}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    executor = object()
    first = background.prepare_in_background(executor, "geo", object())
    second = background.prepare_in_background(executor, "geo", object())
    assert first is second
    assert mcp_prepare.is_preparing("geo"), "visible the moment it is asked for"
    release.set()
    assert set(first.result(_BACKSTOP_S)) == {"geo_x"}
    assert starts == ["geo"]
    state = mcp_prepare.preparation_state("geo")
    assert state is not None and state["state"] == mcp_prepare.READY


def test_a_failure_is_settled_typed_and_the_next_call_starts_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise FileNotFoundError("launcher")
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    executor = object()
    with pytest.raises(FileNotFoundError) as raised:
        background.prepare_in_background(executor, "geo", object()).result(_BACKSTOP_S)
    assert background.failure_reason("geo", raised.value) == mcp_prepare.LAUNCHER_MISSING
    state = mcp_prepare.preparation_state("geo")
    assert state is not None and state["state"] == mcp_prepare.FAILED

    assert background.prepare_in_background(executor, "geo", object()).result(_BACKSTOP_S) == {}
    assert len(attempts) == 2


def test_a_turn_joins_all_missing_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    entered = threading.Event()
    returned = threading.Event()
    results: list[dict[str, str]] = []
    # The previous zero-grace path returned without schemas. This setting must no
    # longer allow a first model request to omit a server that is still listing.
    monkeypatch.setenv("CLIO_MCP_TURN_PREPARE_GRACE_S", "0")

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        if namespace == "geo":
            entered.set()
            release.wait(_BACKSTOP_S)
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)

    def resolve() -> None:
        results.append(background.prepare_for_turn(object(), {"geo": object(), "ndp": object()}))
        returned.set()

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        assert entered.wait(_BACKSTOP_S)
        assert not returned.wait(0.1), "unknown schemas must settle before the request"
    finally:
        release.set()
        worker.join(_BACKSTOP_S)
    assert not worker.is_alive()
    assert results == [{}]


def test_missing_definitions_return_a_settled_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        raise FileNotFoundError("launcher")

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    assert background.prepare_for_turn(object(), {"geo": object()}) == {
        "geo": mcp_prepare.LAUNCHER_MISSING
    }
    assert background.prepare_for_turn(object(), {}) == {}


def test_stop_interrupts_the_wait_without_cancelling_shared_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.errors import CancellationError
    from clio_agent.runtime.cancellation import cancellation_checker

    entered = threading.Event()
    release = threading.Event()

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(_BACKSTOP_S)
        return {"geo_x": object()}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    executor = object()
    spec = object()
    warmup = background.prepare_in_background(executor, "geo", spec)
    try:
        assert entered.wait(_BACKSTOP_S)
        with cancellation_checker(lambda: True), pytest.raises(CancellationError):
            background.prepare_for_turn(executor, {"geo": spec})
        assert not warmup.done() and not warmup.cancelled()
    finally:
        release.set()
        assert set(warmup.result(_BACKSTOP_S)) == {"geo_x"}


class _Fleet:
    """The executor surface the barrier reads and merges into."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        self.tools.update(tools)

    def to_dspy_tools(self) -> list[Any]:
        return list(self.tools.values())


def _stdio_spec() -> Any:
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(transport="stdio", command="uvx", args=["relay"], env={})


def _listing(*names: str) -> list[Any]:
    from mcp.types import Tool  # noqa: PLC0415

    return [Tool(name=name, inputSchema={"type": "object"}) for name in names]


def test_a_listing_persisted_by_a_warm_up_is_adopted_without_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch or workspace warm-up stored the listing after this fleet was built:
    the first request has the tools at once, and no preparation starts on its path."""

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        raise AssertionError("the request path must not prepare a listed server")

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    monkeypatch.setattr(
        "clio_agent.connectors.listing.cached_offered", lambda ns, spec: _listing("ping")
    )
    fleet = _Fleet()
    assert background.prepare_for_turn(fleet, {"geo": _stdio_spec()}) == {}
    assert [tool.name for tool in fleet.to_dspy_tools()] == ["geo_ping"]
    assert not mcp_prepare.is_preparing("geo")


def test_a_turn_joins_another_fleets_preparation_and_returns_once_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn arriving while the launch warm-up prepares the same server does not
    provision it again: it waits for that preparation, adopts its listing, and returns
    while its own fleet's connection is still starting.

    **Sabotage:** no join -> two cold provisions race (the uv launcher lock); no
    listed event -> the first request also waits for this fleet's connect.
    """
    stored: list[Any] = []
    warm_release = threading.Event()
    connect_release = threading.Event()
    calls: list[Any] = []

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        calls.append(executor)
        if executor is warm:
            assert warm_release.wait(_BACKSTOP_S)
            stored.extend(_listing("ping"))  # the warm-up persists its live listing
        else:
            assert connect_release.wait(_BACKSTOP_S)
        return {"geo_ping": object()}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    monkeypatch.setattr("clio_agent.connectors.listing.cached_offered", lambda ns, spec: stored)
    warm, turn = _Fleet(), _Fleet()
    spec = _stdio_spec()
    warmup = background.prepare_in_background(warm, "geo", spec)
    results: list[dict[str, str]] = []
    worker = threading.Thread(
        target=lambda: results.append(background.prepare_for_turn(turn, {"geo": spec}))
    )
    worker.start()
    try:
        worker.join(0.3)
        assert worker.is_alive(), "definitions are unknown until the warm-up lists them"
        assert calls == [warm], "the turn must not start a second provision"
        warm_release.set()
        worker.join(_BACKSTOP_S)
        assert not worker.is_alive()
        assert results == [{}]
        assert [tool.name for tool in turn.to_dspy_tools()] == ["geo_ping"]
        assert not connect_release.is_set(), "returned before its own connect finished"
    finally:
        warm_release.set()
        connect_release.set()
        warmup.result(_BACKSTOP_S)
        with background._lock:
            still = [f for (_, ns), f in background._inflight.items() if ns == "geo"]
        for future in still:
            future.result(_BACKSTOP_S)
