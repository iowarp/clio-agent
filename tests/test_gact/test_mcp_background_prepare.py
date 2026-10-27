"""Background preparation and ``wait_for_tools`` (B3).

A turn never waits for a server it may not use: preparation runs in the background,
every caller joins the one in flight, a finished preparation never keeps showing
``preparing``, and an agent whose server is still starting can wait for it.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from clio_agent.gact import mcp_background_prepare as background
from clio_agent.gact.agents import wait_for_tools
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


def test_a_turn_waits_only_for_the_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def mount(executor: Any, namespace: str, spec: Any) -> dict[str, Any]:
        if namespace == "geo":
            release.wait(_BACKSTOP_S)
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    try:
        preparing, failures = background.prepare_for_turn(
            object(), {"geo": object(), "ndp": object()}, grace_s=0.05
        )
    finally:
        release.set()
    assert preparing == {"geo"}
    assert failures == {}


def test_wait_for_tools_is_offered_for_servers_still_preparing() -> None:
    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_LAUNCH)
    mcp_prepare.record("ndp", mcp_prepare.READY, mcp_prepare.PHASE_LAUNCH)

    waiting = wait_for_tools.preparing_namespaces({"geo", "ndp"}, {})
    assert waiting == ["geo"]
    # A namespace whose tools already reached the agent is not waited for.
    assert wait_for_tools.preparing_namespaces({"geo"}, {"geo_x": object()}) == []

    tool = wait_for_tools.build_wait_for_tools_tool(waiting)
    assert tool.name == wait_for_tools.WAIT_FOR_TOOLS
    assert "geo" in tool.desc


def test_wait_for_tools_reports_each_servers_outcome() -> None:
    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_LAUNCH)
    mcp_prepare.record(
        "ndp", mcp_prepare.FAILED, mcp_prepare.PHASE_PROVISION, reason="r", detail="why"
    )
    timer = threading.Timer(
        0.05,
        mcp_prepare.record,
        args=("geo", mcp_prepare.READY, mcp_prepare.PHASE_LAUNCH),
        kwargs={"tool_count": 3},
    )
    timer.start()
    try:
        result = wait_for_tools.wait_for_servers(["geo", "ndp"], _BACKSTOP_S)
    finally:
        timer.cancel()
    assert result["servers"]["geo"]["state"] == "ready"
    assert result["servers"]["geo"]["tool_count"] == 3
    assert "next step" in result["servers"]["geo"]["note"]
    assert result["servers"]["ndp"] == {
        "state": "failed",
        "phase": "provision",
        "reason": "r",
        "detail": "why",
    }
