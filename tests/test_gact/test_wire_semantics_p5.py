"""Semantic trace reads, ordinary runtime catalogs and immutable transcript views."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import State

from clio_agent.gact.app import build_app
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.types import Message, Part


@contextmanager
def _client(
    tmp_path: Path, *, raise_server_exceptions: bool = True
) -> Iterator[tuple[TestClient, State]]:
    """Enter the real service lifespan and retire its resources after each case."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app, raise_server_exceptions=raise_server_exceptions) as client:
        yield client, app.state


def _session(client: TestClient) -> str:
    """Create one session and return its identifier."""
    response = client.post("/v1/sessions", json={"title": "P5 wire semantics"})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def test_trace_reads_semantic_event_from_arc_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read route folds the canonical ARC log and preserves semantic time."""
    with _client(tmp_path) as (client, state):
        original = state.arc.record_semantic_event
        created: list[str] = []

        def record(event: SemanticEvent) -> Any:
            if event.event_type == "session.created":
                with pytest.raises(RuntimeError, match="no running event loop"):
                    asyncio.get_running_loop()
                created.append(event.session_id)
            return original(event)

        monkeypatch.setattr(state.arc, "record_semantic_event", record)
        sid = _session(client)
        assert created == [sid]
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
        # #1247: every session mints its lifecycle event onto the same highway —
        # pin it, then assert the recorded tool event on the non-lifecycle rest.
        lifecycle = [e for e in events if e["event_type"].startswith("session.")]
        assert [e["event_type"] for e in lifecycle] == ["session.created"]
        events = [e for e in events if not e["event_type"].startswith("session.")]
        assert len(events) == 1
        assert events[0]["event_type"] == "tool.call.completed"
        assert events[0]["occurred_at"] == occurred_at
        assert events[0]["session_id"] == sid
        assert events[0]["turn_id"] == "turn_p5"


def test_session_create_does_not_accept_an_unconfirmed_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Core lifecycle write leaves no newly accepted session row."""
    with _client(tmp_path, raise_server_exceptions=False) as (client, state):
        original = state.arc.record_semantic_event
        before = [row.id for row in state.sessions.list()]
        attempted: list[str] = []

        def reject(event: SemanticEvent) -> Any:
            if event.event_type == "session.created":
                attempted.append(event.session_id)
                raise OSError("Injected lifecycle persistence failure")
            return original(event)

        monkeypatch.setattr(state.arc, "record_semantic_event", reject)
        response = client.post("/v1/sessions", json={"title": "Unconfirmed creation"})
        assert response.status_code == 500, response.text
        assert len(attempted) == 1
        assert [row.id for row in state.sessions.list()] == before
        assert list(state.arc._live.iter_session_event_segments(attempted[0])) == []


def test_trace_unknown_session_returns_typed_404(tmp_path: Path) -> None:
    """Unknown trace sessions use the shared structured not-found envelope."""
    with _client(tmp_path) as (client, _state):
        response = client.get("/v1/sessions/sess_missing/trace")

        assert response.status_code == 404
        assert response.json()["error"]["error"] == "not_found"
        assert response.json()["error"]["details"] == {"session_id": "sess_missing"}


def test_trace_empty_session_returns_empty_events(tmp_path: Path) -> None:
    """A session with no recorded work traces ONLY its own lifecycle event.

    #1247: session creation itself rides the semantic highway, so an
    otherwise-empty session's trace is exactly ``[session.created]`` — never
    more, and never zero (which would mean the lifecycle bridge regressed).
    """
    with _client(tmp_path) as (client, _state):
        sid = _session(client)

        response = client.get(f"/v1/sessions/{sid}/trace")

        assert response.status_code == 200, response.text
        assert [e["event_type"] for e in response.json()["events"]] == ["session.created"]


def test_trace_arc_unavailable_returns_typed_503(tmp_path: Path) -> None:
    """A known session reports the established recoverable ARC degradation.

    Since b80479ea the first semantic event attaches the process ARC, so the session is
    created with an in-memory ARC and the app is then detached (clio-core not attached).
    """
    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.gact.runtime.globals import _set_app_arc

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        arc=ARCMemory(data_dir=str(tmp_path / "arc"), store=_MemoryStore()),
    )
    client = TestClient(app)
    sid = _session(client)
    _set_app_arc(app, None)

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
    with _client(tmp_path) as (client, state):
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
        assert [event["event_type"] for event in response.json()["events"]] == [
            "tool.call.completed"
        ]


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
    with TestClient(app) as client:
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


def test_transcript_reads_preserve_historical_handoffs_without_task_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading history cannot invent terminal output or start a missing legacy owner."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = _session(client)
        session = app.state.sessions.get(sid)
        original = Message(
            id="historical_message",
            session_id=sid,
            turn_id="historical_turn",
            role="assistant",
            created_at=session.created_at,
            updated_at=session.updated_at,
            parts=[
                Part(
                    id="historical_handoff",
                    type="expert_handoff",
                    handle_id="legacy_missing",
                    child_agent="worker",
                    stage="delegate.started",
                    status="running",
                    text="Delegated original analysis",
                )
            ],
        )
        app.state.messages[sid] = [original]
        writes: list[str] = []

        def forbidden_write(*args: object, **kwargs: object) -> None:
            writes.append("unexpected task repair")
            pytest.fail("A transcript read attempted to write task lifecycle")

        monkeypatch.setattr(app.state.task_history, "record_snapshot", forbidden_write)
        first = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        second = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert first == second and len(first) == 1
        handoff = first[0]["parts"][0]
        assert handoff["handle_id"] == "legacy_missing"
        assert handoff["stage"] == "delegate.started" and handoff["status"] == "running"
        assert handoff["text"] == "Delegated original analysis"
        assert writes == [] and not app.state.turn_runner.busy(sid)
        assert app.state.task_history.latest_assignment(sid) is None


def test_special_relay_routes_are_unavailable(tmp_path: Path) -> None:
    """Removed Relay execution is not revived through a capability or status route."""
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        capabilities = client.get("/v1/capabilities").json()["capabilities"]
        assert "relay" not in capabilities
        assert client.get("/v1/relay/status").status_code == 404
