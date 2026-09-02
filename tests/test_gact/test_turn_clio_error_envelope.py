"""#1282 F5/F6 (#1275 asks 2 + 3): a typed ``ClioError`` (a protocol refusal
chief among them) reaching a turn's top-level except-chain must settle with
its OWN reason/details -- never collapse to the generic ``agent_error`` --
and must not leave the expert lifecycle span dangling or skip publishing the
retained History (F6, exercised one layer down at the ReActV2 loop level;
F5 here, at the turn/session envelope level).

Mirrors ``test_finalize_error_envelope.py``'s harness exactly (``build_app`` +
``TestClient`` + poll for a terminal session status), the proven pattern for
asserting on a settled turn's ``error_info``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.errors import MCPMissingRequiredClientCapabilityError
from clio_agent.gact.app import build_app

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _RefusingAgent:
    """A stub agent whose ``forward`` raises a typed MCP protocol refusal --
    stands in for a child turn whose react loop escalated one (#1282 D1)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )


def test_clio_error_settles_the_turn_with_its_own_reason_not_agent_error(
    tmp_path: Path,
) -> None:
    """#1282 F5: the turn's ``error_info`` carries the refusal's OWN typed
    ``error``/``details`` (json_rpc_code, protocol_data naming what to
    re-dial with) -- never the generic ``agent_error`` the pre-fix catch-all
    would have produced."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_RefusingAgent())
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text

        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)
        assert status == "error", f"expected a terminal error status, got {status!r}"

        history = app.state.bus._history.get(sid, [])
        completed = [ev for ev in history if ev.type == "message.completed"]
        assert completed, "a ClioError must still publish a visible error turn"
        error_info = completed[-1].payload["error_info"]

        # #1282 F5: NOT the generic catch-all's "agent_error".
        assert error_info["error"] == "mcp_missing_required_client_capability"
        assert error_info["details"]["reason"] == "mcp_capability_refused"
        assert error_info["details"]["json_rpc_code"] == -32021
        assert (error_info["details"]["protocol_data"]["requiredCapabilities"]["extensions"]) == {
            "io.modelcontextprotocol/tasks": {}
        }
        # #1282 D2: the actionable re-dial hint rides the SAME message the
        # parent sees on the wire.
        assert "io.modelcontextprotocol/tasks" in error_info["message"]

        # ...and in the persisted transcript.
        msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        error_turns = [
            m for m in msgs if m["role"] == "assistant" and m.get("stop_reason") == "error"
        ]
        assert error_turns, "error turn must land in the persisted transcript"
        assert error_turns[0]["error_info"]["error"] == "mcp_missing_required_client_capability"
