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
