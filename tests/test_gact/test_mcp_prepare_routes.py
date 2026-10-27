"""Reconnect for a conversation's plugin MCP servers (B3)."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes import mcp_prepare_routes
from clio_agent.tools import mcp_prepare


class _Executor:
    def __init__(self) -> None:
        self._clio_namespace_specs = {"geo": SimpleNamespace(name="geo")}
        self.resets: list[str] = []

    def reset_namespace(self, namespace: str) -> None:
        self.resets.append(namespace)


def _app(executor: _Executor) -> Any:
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    return SimpleNamespace(state=SimpleNamespace(agent=agent))


@pytest.fixture(autouse=True)
def _session_context(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._tool_session_context", lambda sid: nullcontext()
    )
    yield
    mcp_prepare.forget("geo")


def test_reconnect_stops_the_server_clears_a_failure_and_prepares_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[Any, str]] = []

    def prepare(executor: Any, namespace: str, spec: Any) -> None:
        started.append((executor, namespace))
        mcp_prepare.record(namespace, mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)

    monkeypatch.setattr("clio_agent.gact.mcp_background_prepare.prepare_in_background", prepare)
    mcp_prepare.record(
        "geo", mcp_prepare.FAILED, "provision", reason="mcp_prepare_provision_failed"
    )
    executor = _Executor()

    answer = mcp_prepare_routes.reconnect_session_server(_app(executor), "s1", "geo")

    assert executor.resets == ["geo"]
    assert started == [(executor, "geo")]
    assert answer["state"] == mcp_prepare.PREPARING
    assert answer["preparation"]["state"] == mcp_prepare.PREPARING
    assert "reason" not in answer["preparation"], "the old failure is cleared"


def test_reconnect_of_an_undeclared_server_is_a_typed_404() -> None:
    with pytest.raises(HTTPException) as raised:
        mcp_prepare_routes.reconnect_session_server(_app(_Executor()), "s1", "ghost")
    assert raised.value.status_code == 404
    assert raised.value.detail["error"]["error"] == mcp_prepare_routes.MCP_SERVER_NOT_DECLARED


def test_the_route_answers_for_unknown_sessions(tmp_path: Path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c:
        missing = c.post("/v1/sessions/sess_missing/mcp/geo/reconnect")
    assert missing.status_code == 404


def test_the_server_rows_show_preparation_state_and_typed_failures() -> None:
    from clio_agent.gact.routes.mcp_specs import _session_mcp_server_row
    from clio_agent.tools.mcp_config import MCPServerSpec

    spec = MCPServerSpec(
        name="geo", transport="stdio", command="uv", args=("run",), source="plugin:geo"
    )

    def row(runtime: dict[str, Any]) -> dict[str, Any]:
        return _session_mcp_server_row(
            "geo", spec, runtime=runtime, runtime_unavailable="", session_id="s1", agent_ref="a"
        )

    assert row({})["status"] == "available"
    assert "preparation" not in row({})

    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)
    preparing = row({})
    assert preparing["status"] == "preparing"
    assert preparing["preparation"]["phase"] == "provision"

    mcp_prepare.record(
        "geo", mcp_prepare.FAILED, "provision", reason="mcp_prepare_provision_failed", detail="x"
    )
    failed = row({})
    assert (failed["status"], failed["reason"]) == ("failed", "mcp_prepare_provision_failed")

    # A connected server is ready, whatever an earlier preparation said.
    assert row({"status": "ready", "tools": ["geo_x"]})["status"] == "ready"
