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


def test_trace_unknown_session_returns_typed_404(tmp_path: Path) -> None:
    """Unknown trace sessions use the shared structured not-found envelope."""
    client, _state = _client(tmp_path)

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
    client, _state = _client(tmp_path)
    sid = _session(client)

    response = client.get(f"/v1/sessions/{sid}/trace")

    assert response.status_code == 200, response.text
    assert [e["event_type"] for e in response.json()["events"]] == ["session.created"]


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
        "mcp_url": None,
        "http_url": None,
        "credential_configured": False,
        "configuration_scope": "none",
        "can_manage": True,
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
    assert body["mcp_url"] == "http://relay.example:18783/mcp"
    assert body["http_url"] == "http://relay.example:8765"
    assert body["credential_configured"] is True
    assert body["configuration_scope"] == "server"
    assert "relay-secret" not in response.text


def test_relay_runtime_connection_can_be_attached_and_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The management route applies a non-persistent connection without echoing its secret."""
    monkeypatch.delenv("CLIO_RELAY_MCP_URL", raising=False)
    monkeypatch.delenv("CLIO_RELAY_HTTP_URL", raising=False)
    monkeypatch.delenv("CLIO_RELAY_API_TOKEN", raising=False)

    async def _reachable(host: str, port: int, timeout_seconds: float) -> None:
        assert (host, port, timeout_seconds) == ("relay.lan", 18783, 3.0)

    monkeypatch.setattr("clio_agent.gact.relay_status._tcp_connect", _reachable)
    client, _state = _client(tmp_path)

    attached = client.put(
        "/v1/relay/configuration",
        json={
            "mcp_url": "http://relay.lan:18783/mcp",
            "http_url": "http://relay.lan:8765",
            "access_token": "runtime-secret",
        },
    )

    assert attached.status_code == 200, attached.text
    assert attached.json()["reachable"] is True
    assert attached.json()["configuration_scope"] == "agent_run"
    assert attached.json()["credential_configured"] is True
    assert "runtime-secret" not in attached.text

    detached = client.delete("/v1/relay/configuration")

    assert detached.status_code == 200, detached.text
    assert detached.json()["configured"] is False
    assert detached.json()["configuration_scope"] == "agent_run"
    assert detached.json()["credential_configured"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("mcp_url", "file:///relay.sock"),
        ("http_url", "http://user:secret@relay.lan:8765"),
    ],
)
def test_relay_runtime_connection_rejects_unsafe_addresses(
    tmp_path: Path, field: str, value: str
) -> None:
    """Relay management accepts network URLs and refuses embedded credentials."""
    client, _state = _client(tmp_path)
    payload = {
        "mcp_url": "http://relay.lan:18783/mcp",
        "http_url": "http://relay.lan:8765",
        "access_token": "runtime-secret",
    }
    payload[field] = value

    response = client.put("/v1/relay/configuration", json=payload)

    assert response.status_code == 422
    assert "runtime-secret" not in response.text


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


def _seed_stranded_handoff(
    client: TestClient, state: object, *, task_id: str
) -> tuple[str, object]:
    """Seed a parent session whose turn already ended with a spawned child's
    delegate.started expert_handoff part committed to a STORED message -- the
    "main ended on the circuit breaker without waiting" shape (round-9 wire
    defect, observed live in session sess_539d24da07bf)."""

    from clio_agent.gact.agent_tasks import STATUS_RUNNING, seed_agent_task
    from clio_agent.gact.types import Message, Part

    app = client.app
    parent = _session(client)
    running = seed_agent_task(
        app,
        parent_session_id=parent,
        agent_ref={"expert_id": "ndp_worker", "requesting_expert_id": "main"},
        status=STATUS_RUNNING,
        task_id=task_id,
    )
    now = "2026-08-04T12:00:00+00:00"
    state.messages[parent] = [
        Message(
            id="msg_parent_turn",
            session_id=parent,
            turn_id="msg_parent_turn",
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=[
                Part(
                    id="part_handoff_started",
                    type="expert_handoff",
                    agent_id="main",
                    parent_agent="main",
                    child_agent="ndp_worker",
                    stage="delegate.started",
                    status="running",
                    handle_id=running.handle_id,
                    text="main -> ndp_worker",
                    metadata={"question": "profile the dataset"},
                )
            ],
        )
    ]
    return parent, running


def _fold_completed(app: object, running: object, *, answer: str) -> object:
    """Fold ``running`` to completed via the SAME transport-fold seam a relay
    completion (or the local done-callback) uses -- ``task_fold.fold_agent_task_event``
    runs the winning transition + ALL terminal effects (task_fold.finish_agent_task_transition)."""

    import dataclasses

    from clio_agent.gact import agent_tasks
    from clio_agent.gact.agents.invoker import TaskEvent
    from clio_agent.gact.task_fold import fold_agent_task_event

    completed = dataclasses.replace(
        running,
        status="completed",
        live_state="completed",
        result={"message_ref": "", "answer_excerpt": answer, "workflow_state": {}},
        notify_pending=True,
        updated_at="2026-08-04T12:07:31+00:00",
    )
    event = TaskEvent(
        event_type=agent_tasks.AGENT_TASK_EVENTS[completed.status],
        task_id=completed.task_id,
        session_id=completed.child_session_id,
        status=completed.status,
        payload=dataclasses.asdict(completed),
    )
    return fold_agent_task_event(app, event)


def test_terminal_task_fold_appends_return_after_stored_start(tmp_path: Path) -> None:
    """Round-9 wire defect: a parent turn that ends WITHOUT waiting on a spawned
    child leaves that child's delegate.started expert_handoff part stuck
    "running" forever on the parent's STORED message once the child later
    completes -- neither the next-turn notification drain
    (enrichment.consume_pending_agent_task_notifications) nor a mid-turn inbox
    drain (loop_inbox) ever runs for a parent that gets no further turn. The
    task-terminal-transition seam (task_fold.finish_agent_task_transition ->
    background_exit.reconcile_stored_handoff_part) must append the return to the
    STORED ledger and publish message.part.added so an already-open client sees
    the completed lifecycle without history being rewritten."""

    client, state = _client(tmp_path)
    parent, running = _seed_stranded_handoff(client, state, task_id="task_stranded")

    outcome = _fold_completed(client.app, running, answer="3 stations profiled")
    assert outcome.applied is True

    started, returned = state.messages[parent][0].parts
    assert started.id == "part_handoff_started"
    assert started.stage == "delegate.started"
    assert started.metadata["question"] == "profile the dataset"
    assert "output" not in started.metadata
    assert returned.stage == "delegate.completed"
    assert returned.status == "completed"
    assert returned.metadata["output"] == "3 stations profiled"
    assert "question" not in returned.metadata

    # A client already watching the session gets the correction pushed over SSE.
    additions = [e for e in state.bus._history.get(parent, []) if e.type == "message.part.added"]
    assert len(additions) == 1
    assert additions[0].payload["message_id"] == "msg_parent_turn"
    assert additions[0].payload["part"]["stage"] == "delegate.completed"

    # GET /messages agrees -- no more lying "running" render on a fresh load.
    fetched = client.get(f"/v1/sessions/{parent}/messages").json()["messages"]
    handoffs = [p for m in fetched for p in m["parts"] if p["type"] == "expert_handoff"]
    assert [row["stage"] for row in handoffs] == ["delegate.started", "delegate.completed"]
    assert handoffs[1]["status"] == "completed"


def test_terminal_task_fold_stored_handoff_writeback_is_idempotent(tmp_path: Path) -> None:
    """A later fold/wait reaching the SAME terminal task must not double-write or
    double-publish the stored parent message's terminal handoff part."""

    from clio_agent.gact.background_exit import reconcile_stored_handoff_part

    client, state = _client(tmp_path)
    parent, running = _seed_stranded_handoff(client, state, task_id="task_stranded_idem")

    outcome = _fold_completed(client.app, running, answer="done")
    assert outcome.applied is True
    before_additions = len(
        [e for e in state.bus._history.get(parent, []) if e.type == "message.part.added"]
    )

    # Simulate a later consumer reaching this SAME terminal task again (a
    # retried fold, or a duplicate transport observation).
    again = reconcile_stored_handoff_part(client.app, outcome.task)

    after_additions = len(
        [e for e in state.bus._history.get(parent, []) if e.type == "message.part.added"]
    )
    assert again is False
    assert after_additions == before_additions
    assert len(state.messages[parent][0].parts) == 2
    assert [part.stage for part in state.messages[parent][0].parts] == [
        "delegate.started",
        "delegate.completed",
    ]


def _seed_historically_stale_handoff(client: TestClient, *, task_id: str) -> tuple[str, object]:
    """Seed a parent session shaped like it PREDATES the terminal-transition
    writeback (f7066068) entirely: the child task is already terminal in the
    agent-task registry (seeded straight to ``completed``, bypassing
    ``fold_agent_task_transition``/``finish_agent_task_transition`` so
    ``reconcile_stored_handoff_part`` never ran for it), and its
    ``delegate.started`` part is written through the REAL durable session
    store -- not just the resident cache -- so a cold-start reload actually
    rehydrates it from disk, exactly like a real historical session would."""

    from clio_agent.gact.agent_tasks import STATUS_COMPLETED, seed_agent_task
    from clio_agent.gact.session_store import _replace_session_messages
    from clio_agent.gact.types import Message, Part

    app = client.app
    parent = _session(client)
    completed = seed_agent_task(
        app,
        parent_session_id=parent,
        agent_ref={"expert_id": "ndp_worker", "requesting_expert_id": "main"},
        status=STATUS_COMPLETED,
        task_id=task_id,
    )
    now = "2026-08-04T10:10:47+00:00"
    _replace_session_messages(
        app,
        parent,
        [
            Message(
                id="msg_parent_turn",
                session_id=parent,
                turn_id="msg_parent_turn",
                role="assistant",
                created_at=now,
                updated_at=now,
                parts=[
                    Part(
                        id="part_handoff_started",
                        type="expert_handoff",
                        agent_id="main",
                        parent_agent="main",
                        child_agent="ndp_worker",
                        stage="delegate.started",
                        status="running",
                        handle_id=completed.handle_id,
                        text="main -> ndp_worker",
                        metadata={"question": "profile the dataset"},
                    )
                ],
            )
        ],
    )
    return parent, completed


def test_stale_handoff_reconciled_lazily_on_session_load(tmp_path: Path) -> None:
    """D1: a session whose child completed BEFORE the terminal-transition
    writeback existed keeps a delegate.started expert_handoff part frozen
    "running" forever -- GET /messages never re-runs
    task_fold.finish_agent_task_transition, so the live writeback (f7066068)
    never reaches it. The lazy reconcile-on-load sweep
    (background_exit.sweep_stale_handoff_parts, wired at ResidentLedgerSet's
    on_rehydrate seam) must close it the first time the session is (re)loaded
    after a cold start -- simulated here via discard() (drop the resident copy
    only) + a real GET, which forces rehydration from the durable store -- and
    record ONE typed handoff.reconciled.stale provenance row."""

    client, state = _client(tmp_path)
    parent, completed = _seed_historically_stale_handoff(client, task_id="task_predates_writeback")

    assert list(getattr(state, "handoff_reconciliations", [])) == []

    # Cold-start simulation: only the RESIDENT copy is dropped -- the durable
    # store (what a real restart rehydrates from) still has the stale part.
    state.messages.discard(parent)

    fetched = client.get(f"/v1/sessions/{parent}/messages").json()["messages"]
    handoffs = [p for m in fetched for p in m["parts"] if p["type"] == "expert_handoff"]
    assert [row["stage"] for row in handoffs] == ["delegate.started", "delegate.completed"]
    assert handoffs[1]["status"] == "completed"

    events = list(state.handoff_reconciliations)
    assert len(events) == 1
    assert events[0]["event"] == "handoff.reconciled.stale"
    assert events[0]["session_id"] == parent
    assert events[0]["handle_id"] == completed.handle_id
    assert events[0]["task_status"] == "completed"


def test_stale_handoff_reconcile_sweep_is_idempotent_across_reloads(tmp_path: Path) -> None:
    """The sweep must not re-fire (double-write the part or double-record the
    typed event) on a session that was already reconciled by an earlier load
    -- the idempotency twin of the reconcile-on-load test above."""

    client, state = _client(tmp_path)
    parent, completed = _seed_historically_stale_handoff(client, task_id="task_predates_idem")

    state.messages.discard(parent)
    first = client.get(f"/v1/sessions/{parent}/messages").json()["messages"]
    first_handoffs = [p for m in first for p in m["parts"] if p["type"] == "expert_handoff"]
    assert [row["stage"] for row in first_handoffs] == [
        "delegate.started",
        "delegate.completed",
    ]
    assert len(state.handoff_reconciliations) == 1

    # A second cold load of the SAME (now-reconciled, durably-persisted) session.
    state.messages.discard(parent)
    second = client.get(f"/v1/sessions/{parent}/messages").json()["messages"]
    second_handoffs = [p for m in second for p in m["parts"] if p["type"] == "expert_handoff"]

    assert [row["stage"] for row in second_handoffs] == [
        "delegate.started",
        "delegate.completed",
    ]
    assert second_handoffs[1]["status"] == "completed"
    # No re-write, no second typed event.
    assert len(state.handoff_reconciliations) == 1
    assert state.handoff_reconciliations[0]["handle_id"] == completed.handle_id
