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
    assert response.json()["relay"] == {
        "configured": False,
        "host": None,
        "reason": "relay_tools_not_configured",
        "details": {"missing": ["api_token", "http_url", "mcp_url"]},
    }


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
        "detail": "relay_tools_not_configured: relay transport configuration is incomplete",
        "reason": "relay_tools_not_configured",
        "details": {"missing": ["api_token", "http_url", "mcp_url"]},
    }


def test_relay_status_reports_mocked_tcp_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured status reports the result of the bounded TCP probe."""
    monkeypatch.setenv("CLIO_RELAY_MCP_URL", "http://relay.example:18783/mcp")
    monkeypatch.setenv("CLIO_RELAY_HTTP_URL", "http://relay.example:8765")
    monkeypatch.setenv("CLIO_RELAY_API_TOKEN", "relay-secret")

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


def test_blueprint_tools_validate_against_serve_runtime_catalog(tmp_path):
    """Declared serve-mounted tools (relay/federation) validate ONLY with the runtime catalog (#P5 run-2 gap)."""
    from pathlib import Path

    from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path

    root = tmp_path / "bp"
    (root / "experts").mkdir(parents=True)
    (root / "AGENT.md").write_text(
        "---\nid: bp\ntitle: BP\nversion: 0.1.0\ndescription: t\nroot_expert: main\n"
        "blueprint:\n  format: agent-blueprint-v1\nexperts:\n  - experts/main.md\n---\nbody\n",
        encoding="utf-8",
    )
    (root / "experts" / "main.md").write_text(
        "---\nid: main\ntitle: M\ntier: 1\nrole: orchestrator\nmodule:\n  kind: react\n"
        "signature:\n  inputs:\n    question:\n      description: q\n      type: string\n"
        "  outputs:\n    answer:\n      description: a\n      type: string\n"
        "tools:\n  - remote_scientific_jarvis_jarvis_run\n---\nbody\n",
        encoding="utf-8",
    )
    # Without the runtime catalog: unknown tool -> root disabled (the old failure).
    cold = validate_agent_blueprint_path(Path(root), scope="session")
    cold_main = next(r for r in cold["agents"] if r["id"] == "main")
    assert not cold_main["enabled"]
    assert any("unknown tool reference" in e for e in cold_main["validation_errors"])
    # With it: valid, carrying the typed serve_runtime provenance diagnostic.
    warm = validate_agent_blueprint_path(
        Path(root),
        scope="session",
        runtime_tool_names={"remote_scientific_jarvis_jarvis_run"},
    )
    warm_main = next(r for r in warm["agents"] if r["id"] == "main")
    assert warm_main["enabled"]
    diags = warm_main.get("metadata", {}).get("tool_diagnostics", [])
    assert any(d.get("source") == "serve_runtime" for d in diags if isinstance(d, dict))


class _AckAgent:
    """Minimal host agent -- the missing-parts request must 400 before this runs."""

    def forward(self, question: str, session_id: str, **_kwargs: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


def test_post_message_missing_parts_is_typed_400_validation_error(tmp_path: Path) -> None:
    """Round-9 wire defect: {"content": "..."} (missing parts[]/text) is an
    unrecognized shape -- extract_text() legitimately returns "", so the route
    must reject it as a typed CLIENT validation error, never "internal_error"
    (that tag is reserved for a >=500 server-side fault, see
    ``_error_code_for_status`` in app.py)."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_AckAgent())
    client = TestClient(app)
    sid = _session(client)

    response = client.post(f"/v1/sessions/{sid}/messages", json={"content": "hi there"})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["error"] == "validation_error"
    assert "parts" in error["message"]
    assert error["details"] == {"session_id": sid}
    assert error["recoverable"] is True
    # No user message was persisted for the rejected body.
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
