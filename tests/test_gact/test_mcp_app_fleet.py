"""A view whose server's fleet was closed is not live, and its UI is told (fix stream).

Seen live (q-pv): a conversation nobody viewed for two minutes had its fleet reaped;
the pinned ParaView view stayed "live" in the registry, its card offered "Show tab",
and mounting it met a dead service. The registry now drops such a record with the
typed reason ``mcp_app_server_stopped`` and publishes ``mcp_app_closed``, so the card
offers Reopen (MA19). Every other server-side close is published the same way.
"""

from __future__ import annotations

import threading
import weakref
from types import SimpleNamespace
from typing import Any

from clio_agent.gact import mcp_app_fleet
from clio_agent.gact.mcp_app_cleanup import APP_CLOSED_EVENT, publish_closed
from clio_agent.gact.mcp_apps import MCPAppRegistry


class _Fleet:
    closed = False


def _registry() -> tuple[MCPAppRegistry, list[tuple[str, str]]]:
    registry = MCPAppRegistry(limit=8, ttl_s=3600)
    closed: list[tuple[str, str]] = []
    registry.on_closed = lambda record, reason: closed.append((record.app_instance_id, reason))
    return registry, closed


def _open(registry: MCPAppRegistry, sid: str = "sess_a") -> Any:
    return registry.register(
        session_id=sid,
        source_namespace="viz",
        tool_name="viz_open",
        resource_uri="ui://viz/view",
        tool_input={},
        tool_result={"content": [{"type": "text", "text": "opened"}]},
        display_mode="fullscreen",
    )


def test_a_pinned_view_whose_fleet_closed_is_dropped_and_announced() -> None:
    registry, closed = _registry()
    fleet = _Fleet()
    record = _open(registry)
    record.fleet = weakref.ref(fleet)
    assert registry.get("sess_a", record.app_instance_id, record.data_ref) is record

    fleet.closed = True  # the reaper (or a configuration change) closed the fleet
    assert registry.records_for_session("sess_a") == []
    assert closed == [(record.app_instance_id, "server_stopped")]


def test_an_unstamped_record_is_never_dropped_on_a_guess() -> None:
    registry, closed = _registry()
    record = _open(registry)
    assert mcp_app_fleet.server_stopped(record) is False
    assert registry.records_for_session("sess_a") == [record]
    assert closed == []


def test_a_collected_fleet_counts_as_stopped() -> None:
    record = SimpleNamespace(fleet=weakref.ref(_Fleet()))  # collected at once
    assert mcp_app_fleet.server_stopped(record) is True


def test_admission_stamps_the_workspace_fleet_that_serves_the_session(tmp_path: Any) -> None:
    fleet = _Fleet()
    root = str(tmp_path)
    from clio_agent.tools.workspace_root import canonical_workspace_root

    agent = SimpleNamespace(
        _workspace_state=lambda: (threading.Lock(), {canonical_workspace_root(root): fleet}, {})
    )
    session = SimpleNamespace(workspace_id="ws_1")
    workspace = SimpleNamespace(root_path=root)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda sid: session),
            workspaces=SimpleNamespace(get=lambda wid: workspace),
        )
    )
    record = SimpleNamespace(session_id="sess_a", fleet=None)
    mcp_app_fleet.admitted(app, record)
    assert record.fleet() is fleet
    fleet.closed = True
    assert mcp_app_fleet.server_stopped(record) is True


def test_every_server_side_close_is_announced() -> None:
    registry, closed = _registry()
    first, second, third = _open(registry), _open(registry), _open(registry, "sess_b")
    registry.remove(first.app_instance_id)
    registry.drop_session("sess_a")
    registry.claim_close("sess_b", third.app_instance_id, third.data_ref)
    assert closed == [
        (first.app_instance_id, "closed"),
        (second.app_instance_id, "session_ended"),
        (third.app_instance_id, "closed"),
    ]


def test_the_close_notice_is_a_transient_session_event() -> None:
    events: list[Any] = []
    app = SimpleNamespace(state=SimpleNamespace(bus=SimpleNamespace(publish=events.append)))
    record = SimpleNamespace(session_id="sess_a", app_instance_id="app_1")
    publish_closed(app, record, "server_stopped")
    (event,) = events
    assert event.type == APP_CLOSED_EVENT
    assert event.session_id == "sess_a"
    assert event.payload == {
        "session_id": "sess_a",
        "app_instance_id": "app_1",
        "reason": "server_stopped",
    }
