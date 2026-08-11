"""#1201 fix-round finding #1: the era-downgrade record must reach a real
operator/session surface, not just the tools-layer ring with zero readers.

Covers:
* providers/handshake/mcp.py -- MCPServerReport.execution_era + the
  to_integration_status() row it produces (finding #1a).
* gact/routes/mcp_rows.py -- handshake_server_row surfaces the same fields on
  the actual /v1/mcp/handshake wire response (finding #1a).
* gact/mcp_connection_observability.py -- emit_downgrade_events_for_executor
  reaches the session's semantic-event trace, once per (session, server)
  (finding #1b).
* _record_downgrade also calls stream_audit (finding #1c) -- exercised
  indirectly by asserting no exception and by the ring itself (a dedicated
  stream_audit-file assertion lives in test_mcp_connection_era.py's
  legacy-under-auto test via the shared module import).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from clio_agent.errors import MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
from clio_agent.gact.mcp_connection_observability import emit_downgrade_events_for_executor
from clio_agent.gact.routes.mcp_rows import handshake_server_row
from clio_agent.providers.handshake.mcp import MCPServerReport
from clio_agent.providers.handshake.model import ConnectivityState
from clio_agent.runtime.status import IntegrationState
from clio_agent.tools.mcp_connection_era import classify_connection_era


def _downgraded_era(server_id: str):
    return classify_connection_era(
        server_id=server_id, protocol_version="2025-06-18", connect_mode="auto"
    )


def _healthy_era(server_id: str):
    return classify_connection_era(
        server_id=server_id, protocol_version="2026-07-28", connect_mode="auto"
    )


# --------------------------------------------------------------------------- #
# finding #1a: MCPServerReport.execution_era + to_integration_status()
# --------------------------------------------------------------------------- #


def test_report_with_no_execution_history_is_unaffected():
    """A server never observed on any execution path reports normally."""
    report = MCPServerReport(
        name="quiet", connectivity=ConnectivityState.OK, transport="stdio", tool_count=2
    )
    status = report.to_integration_status()
    assert "execution_era" not in status.details
    assert status.state == IntegrationState.READY


def test_report_surfaces_a_real_execution_downgrade_even_when_probe_is_healthy():
    """This probe's OWN connect can be modern while live traffic downgraded --
    doctor must show the REAL history, not just this instant's diagnostic."""
    era = _downgraded_era("flaky-server")
    report = MCPServerReport(
        name="flaky-server",
        connectivity=ConnectivityState.OK,
        transport="stdio",
        tool_count=3,
        protocol_version="2026-07-28",  # THIS probe's own connect: healthy
        execution_era=era,  # but live traffic downgraded
    )

    status = report.to_integration_status()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["execution_era"] == "legacy"
    assert status.details["execution_downgrade_reason"] == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    assert "downgrade" in status.summary.lower()
    assert "connect_mode" in status.next_action.lower() or "auto" in status.next_action.lower()


def test_report_with_healthy_execution_era_stays_ready():
    """A server whose real traffic never downgraded reports READY, unchanged."""
    era = _healthy_era("solid-server")
    report = MCPServerReport(
        name="solid-server",
        connectivity=ConnectivityState.OK,
        transport="stdio",
        tool_count=1,
        protocol_version="2026-07-28",
        execution_era=era,
    )

    status = report.to_integration_status()

    assert status.state == IntegrationState.READY
    assert status.details["execution_era"] == "modern"
    assert "execution_downgrade_reason" not in status.details


# --------------------------------------------------------------------------- #
# finding #1a: the actual /v1/mcp/handshake wire row (routes/mcp_rows.py)
# --------------------------------------------------------------------------- #


def test_handshake_wire_row_surfaces_execution_downgrade():
    era = _downgraded_era("wire-server")
    report = MCPServerReport(
        name="wire-server",
        connectivity=ConnectivityState.OK,
        transport="stdio",
        tool_count=1,
        tools=("t1",),
        protocol_version="2026-07-28",
        execution_era=era,
    )

    row = handshake_server_row(report)

    assert row["execution_era"] == "legacy"
    assert row["execution_downgrade_reason"] == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    assert row["protocol_version"] == "2026-07-28"  # this probe's own connect, unchanged


def test_handshake_wire_row_absent_execution_history_is_none():
    report = MCPServerReport(name="fresh", connectivity=ConnectivityState.OK, transport="stdio")
    row = handshake_server_row(report)
    assert row["execution_era"] is None
    assert row["execution_downgrade_reason"] is None


# --------------------------------------------------------------------------- #
# finding #1b: session semantic-event surfacing
# --------------------------------------------------------------------------- #


class _FakeExecutor:
    def __init__(self, namespaces: tuple[str, ...]) -> None:
        self._namespaces = namespaces

    def namespaces(self) -> tuple[str, ...]:
        return self._namespaces


def test_emit_downgrade_events_emits_once_per_session_and_server(monkeypatch):
    """A downgraded namespace on the executor emits a semantic event; a
    healthy namespace does not; re-resolving the SAME executor in the SAME
    session does not re-emit."""
    _downgraded_era("obs-downgraded")
    _healthy_era("obs-healthy")
    executor = _FakeExecutor(("obs-downgraded", "obs-healthy"))

    calls: list[dict[str, Any]] = []

    def _fake_emit(app, sid, event_type, **kwargs):
        calls.append({"sid": sid, "event_type": event_type, **kwargs})
        return {}

    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._emit_semantic_event",
        _fake_emit,
    )

    app = MagicMock()
    emit_downgrade_events_for_executor(app, "session-1", executor)

    assert len(calls) == 1
    assert calls[0]["event_type"] == "mcp.connection.downgraded"
    assert calls[0]["sid"] == "session-1"
    assert calls[0]["payload"]["server_id"] == "obs-downgraded"
    assert calls[0]["payload"]["reason"] == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY

    # Re-resolving the same executor in the same session: no duplicate event.
    emit_downgrade_events_for_executor(app, "session-1", executor)
    assert len(calls) == 1

    # A DIFFERENT session with the same downgraded server DOES get its own event.
    emit_downgrade_events_for_executor(app, "session-2", executor)
    assert len(calls) == 2
    assert calls[1]["sid"] == "session-2"


def test_emit_downgrade_events_is_a_noop_without_a_session_or_executor():
    emit_downgrade_events_for_executor(MagicMock(), "", _FakeExecutor(("x",)))  # no sid
    emit_downgrade_events_for_executor(MagicMock(), "sid", None)  # no executor
    emit_downgrade_events_for_executor(MagicMock(), "sid", object())  # no namespaces()
    # None of the above should raise.


@pytest.mark.asyncio
async def test_relay_transport_client_classifies_under_relay_server_id(monkeypatch):
    """tools/relay_transport.py's direct connect (#1201 bypass site) is
    classified under the fixed 'relay' server id. httpx.AsyncClient is real
    (harmless to construct/close without ever issuing a request); only the
    MCP client factory is faked, to avoid a real network connect."""
    from clio_agent.tools.mcp_connection_era import instrument_client_era, latest_mcp_connection_era
    from clio_agent.tools.relay_transport import RelayTransportClient

    class _FakeMcpClient:
        protocol_version = "2026-07-28"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    def fake_make_mcp_client(target, **kwargs):
        assert kwargs.get("server_id") == "relay"
        return instrument_client_era(_FakeMcpClient(), server_id=kwargs["server_id"])

    monkeypatch.setattr("clio_agent.tools.relay_transport.make_mcp_client", fake_make_mcp_client)

    client = RelayTransportClient(
        mcp_url="https://relay.example/mcp",
        http_base_url="https://relay.example",
        api_token="tok",
    )
    async with client:
        pass

    era = latest_mcp_connection_era("relay")
    assert era is not None
    assert era.era == "modern"
