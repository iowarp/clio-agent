"""P5 semantic-trace read and relay-reachability wire contracts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.semantic_events import SemanticEvent


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    """Build one test app and return its client plus application state."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    return TestClient(app), app.state


def _session(client: TestClient) -> str:
    """Create one session and return its identifier."""
    response = client.post("/v1/sessions", json={"title": "P5 wire semantics"})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def test_trace_reads_semantic_event_from_arc_log(tmp_path: Path) -> None:
    """The read route folds the canonical ARC log and preserves semantic time."""
    client, state = _client(tmp_path)
    sid = _session(client)
    occurred_at = "2026-08-04T12:34:56.123456+00:00"
    state.arc.record_semantic_event(
        SemanticEvent(
            event_type="tool.call.completed",
            session_id=sid,
            trace_id="trace_p5",
            turn_id="turn_p5",
            occurred_at=occurred_at,
            payload={"tool_name": "pandas_profile_csv"},
        )
    )

    response = client.get(f"/v1/sessions/{sid}/trace")

    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "tool.call.completed"
    assert events[0]["occurred_at"] == occurred_at
    assert events[0]["session_id"] == sid
    assert events[0]["turn_id"] == "turn_p5"


def test_trace_unknown_session_returns_typed_404(tmp_path: Path) -> None:
    """Unknown trace sessions use the shared structured not-found envelope."""
    client, _state = _client(tmp_path)

    response = client.get("/v1/sessions/sess_missing/trace")

    assert response.status_code == 404
    assert response.json()["error"]["error"] == "not_found"
    assert response.json()["error"]["details"] == {"session_id": "sess_missing"}


def test_trace_empty_session_returns_empty_events(tmp_path: Path) -> None:
    """A known session with no semantic log records has an empty trace."""
    client, _state = _client(tmp_path)
    sid = _session(client)

    response = client.get(f"/v1/sessions/{sid}/trace")

    assert response.status_code == 200, response.text
    assert response.json() == {"events": []}


def test_trace_arc_unavailable_returns_typed_503(tmp_path: Path) -> None:
    """A known session reports the established recoverable ARC degradation."""
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None)
    client = TestClient(app)
    sid = _session(client)

    response = client.get(f"/v1/sessions/{sid}/trace")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "error": "arc_unavailable",
        "message": "ARC memory is not enabled for this deployment",
        "details": {"session_id": sid},
        "recoverable": True,
    }


def test_trace_scope_and_limit_keep_latest_matches_oldest_first(tmp_path: Path) -> None:
    """Scope uses semantic namespaces and limit retains chronological output."""
    client, state = _client(tmp_path)
    sid = _session(client)
    for index, event_type in enumerate(
        ("tool.call.started", "memory.search.completed", "tool.call.completed")
    ):
        state.arc.record_semantic_event(
            SemanticEvent(
                event_type=event_type,
                session_id=sid,
                trace_id="trace_p5",
                occurred_at=f"2026-08-04T12:34:5{index}+00:00",
            )
        )

    response = client.get(f"/v1/sessions/{sid}/trace", params={"scope": "tool", "limit": 1})

    assert response.status_code == 200, response.text
    assert [event["event_type"] for event in response.json()["events"]] == ["tool.call.completed"]


def test_capabilities_carries_unconfigured_relay_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capabilities reports endpoint configuration without probing relay."""
    monkeypatch.delenv("CLIO_RELAY_MCP_URL", raising=False)
    client, _state = _client(tmp_path)

    response = client.get("/v1/capabilities")

    assert response.status_code == 200, response.text
    assert response.json()["relay"] == {"configured": False, "host": None}


def test_relay_status_unconfigured_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent relay endpoint is explicit and has no fabricated reachability."""
    monkeypatch.delenv("CLIO_RELAY_MCP_URL", raising=False)
    client, _state = _client(tmp_path)

    response = client.get("/v1/relay/status")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "configured": False,
        "host": None,
        "reachable": None,
        "checked_at": None,
        "detail": "relay is not configured: CLIO_RELAY_MCP_URL is unset",
    }


def test_relay_status_reports_mocked_tcp_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured status reports the result of the bounded TCP probe."""
    monkeypatch.setenv("CLIO_RELAY_MCP_URL", "http://relay.example:18783/mcp")

    async def _reachable(host: str, port: int, timeout_seconds: float) -> None:
        assert (host, port, timeout_seconds) == ("relay.example", 18783, 3.0)

    monkeypatch.setattr("clio_agent.gact.relay_status._tcp_connect", _reachable)
    client, _state = _client(tmp_path)

    response = client.get("/v1/relay/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["host"] == "relay.example"
    assert body["reachable"] is True
    assert datetime.fromisoformat(body["checked_at"])
    assert body["detail"] == "TCP connect to relay.example:18783 succeeded"
