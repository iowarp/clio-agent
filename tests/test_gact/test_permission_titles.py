"""A permission card and its waiting row name an external MCP tool by its own title.

First-run journey (apps ledger, "First-run journey" JR-F1): a person was asked to allow
"external MCP tool call: paraview_open_view", with the same raw name on the waiting row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents import tool_instrumentation
from clio_agent.gact.events import Event
from clio_agent.gact.permission_delivery import permission_summary, publish_waiting_call
from clio_agent.gact.protocol.v3.event import _tool_waiting


@pytest.fixture
def titled(monkeypatch: pytest.MonkeyPatch) -> None:
    presentations = dict(tool_instrumentation._TOOL_PRESENTATIONS)
    presentations["viewer_set_timestep"] = ("default", "Change the timestep")
    presentations["viewer_untitled"] = ("default", "")
    monkeypatch.setattr(tool_instrumentation, "_TOOL_PRESENTATIONS", presentations)


EXTERNAL = "external MCP tool"


def test_external_tool_reads_as_its_title(titled: None) -> None:
    context = {"kind": "external_mcp", "annotations": {}}
    assert permission_summary("viewer_set_timestep", context, EXTERNAL) == "Change the timestep"


def test_untitled_external_tool_keeps_the_plain_line(titled: None) -> None:
    assert (
        permission_summary("viewer_untitled", None, EXTERNAL)
        == "external MCP tool call: viewer_untitled"
    )


def test_native_summary_wins_and_native_tools_keep_their_line(titled: None) -> None:
    native = {"kind": "native", "summary": "Run command ls"}
    assert permission_summary("viewer_set_timestep", native, EXTERNAL) == "Run command ls"
    # A destructive native tool keeps the line the UI maps to its own label.
    assert (
        permission_summary("viewer_set_timestep", None, "destructive tool")
        == "destructive tool call: viewer_set_timestep"
    )


class _Bus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


def test_waiting_row_carries_the_title(titled: None) -> None:
    bus = _Bus()
    app = SimpleNamespace(state=SimpleNamespace(bus=bus, turn_transcripts=None))
    row: dict[str, Any] = {
        "id": "perm_1",
        "session_id": "sess_1",
        "created_at": "2026-11-13T00:00:00+00:00",
        "tool_call": {"tool_name": "viewer_set_timestep", "input": {"t": 3}, "call_id": "c1"},
    }
    publish_waiting_call(app, row)
    (event,) = bus.events
    assert event.payload["tool_title"] == "Change the timestep"
    projected = _tool_waiting(event, dict(event.payload), None)
    assert projected.payload["title"] == "Change the timestep"
    assert projected.payload["name"] == "viewer_set_timestep"


def test_a_snapshot_of_a_waiting_call_carries_the_title(titled: None) -> None:
    from clio_agent.gact.permission_delivery import overlay_waiting_calls

    row = {
        "id": "perm_1",
        "session_id": "sess_1",
        "status": "pending",
        "tool_call": {"tool_name": "viewer_set_timestep", "input": {}, "call_id": "c1"},
    }
    app = SimpleNamespace(state=SimpleNamespace(permissions={"perm_1": row}))
    (tool,) = overlay_waiting_calls(app, "sess_1", [])
    assert tool["title"] == "Change the timestep"
    assert tool["state"] == "pending"


def test_a_recorded_waiting_call_gains_the_title(titled: None) -> None:
    from clio_agent.gact.permission_delivery import overlay_waiting_calls

    row = {
        "id": "perm_1",
        "session_id": "sess_1",
        "status": "pending",
        "tool_call": {"tool_name": "viewer_set_timestep", "input": {}, "call_id": "c1"},
    }
    app = SimpleNamespace(state=SimpleNamespace(permissions={"perm_1": row}))
    recorded = [{"id": "c1", "name": "viewer_set_timestep", "state": "running"}]
    (tool,) = overlay_waiting_calls(app, "sess_1", recorded)
    assert (tool["state"], tool["title"]) == ("pending", "Change the timestep")


def _projected(kind: str | None) -> dict[str, Any]:
    from clio_agent.gact.routes.interactions import _permission_interaction

    app = SimpleNamespace(
        state=SimpleNamespace(sessions=None, workspaces=None, task_history=None, agent_tasks=None)
    )
    row: dict[str, Any] = {
        "id": "perm_1",
        "session_id": "sess_1",
        "status": "pending",
        "summary": "Open a view",
        "task_id": "task_1",
        "tool_call": {"tool_name": "viewer_open", "input": {"path": "/remote/data.vti"}},
    }
    if kind:
        row["kind"] = kind
    return _permission_interaction(app, row).payload


def test_an_external_tool_card_does_not_claim_the_workspace_host() -> None:
    # The connector's server decides where the call runs (another machine, a hosted service).
    assert "execution" not in _projected("external_mcp")
    assert set(_projected(None)["execution"]) == {"computer", "cwd"}
