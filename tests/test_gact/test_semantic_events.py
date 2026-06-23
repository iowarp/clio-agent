"""Research-grade semantic execution event stream."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_tool_observer, build_app
from clio_agent.runtime.hooks import HookRegistry, install_global_registry


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred()


@dataclass
class _DiffPred(_Pred):
    file_diffs: list = field(default_factory=list)


class _DiffAgent:
    def forward(self, question: str, session_id: str):
        return _DiffPred(
            file_diffs=[
                {
                    "path": "result.txt",
                    "unified_diff": "--- a/result.txt\n+++ b/result.txt\n@@\n-old\n+new\n",
                    "new_content": "new\n",
                }
            ]
        )


def test_semantic_events_stream_and_trace_file(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(trace_dir))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "analyze this")

    history = app.state.bus._history.get(sid, [])
    semantic_events = [e for e in history if e.type == "semantic.event"]
    event_types = [e.payload["event_type"] for e in semantic_events]
    assert "turn.started" in event_types
    assert "hook.invocation.started" in event_types
    assert "agent.invocation.started" in event_types
    assert "llm.request.started" in event_types
    assert "llm.response.completed" in event_types
    assert "agent.invocation.completed" in event_types
    assert "turn.completed" in event_types
    assert semantic_events[-1].payload["detail_level"] == "semantic"
    assert all(e.payload["trace_id"].startswith("trace_msg_user_") for e in semantic_events)
    assert all(e.payload["payload"].get("ui_summary") for e in semantic_events)
    turn_completed = next(
        e.payload for e in semantic_events if e.payload["event_type"] == "turn.completed"
    )
    assert turn_completed["payload"]["result_summary"] == turn_completed["summary"]

    completed_idx = next(i for i, e in enumerate(history) if e.type == "message.completed")
    semantic_completed_idx = next(
        i
        for i, e in enumerate(history)
        if e.type == "semantic.event" and e.payload["event_type"] == "turn.completed"
    )
    assert semantic_completed_idx < completed_idx

    app.state.semantic_trace_backend.flush()  # off-loop writer: drain before reading
    trace_path = trace_dir / f"{sid}.semantic.jsonl"
    rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    # ARC is the source: with a real ARC wired, its op-logger also mirrors each ARC
    # write onto the durable trace as ``arc.op`` rows (interleaved with the semantic
    # events). Filter them out to assert the leading SEMANTIC event sequence.
    semantic_rows = [row for row in rows if row["event_type"] != "arc.op"]
    assert [row["event_type"] for row in semantic_rows[:3]] == [
        "turn.started",
        "hook.invocation.started",
        "hook.invocation.completed",
    ]
    # The DURABLE canonical trace captures FULL (unredacted) bodies regardless of
    # the SSE detail_level; redaction is an SSE-only projection (asserted below).
    request_row = next(row for row in rows if row["event_type"] == "llm.request.started")
    assert request_row["payload"]["input"] == "analyze this"
    # ...while the live SSE stream for the same event stays redacted at "semantic".
    sse_request = next(
        e.payload for e in semantic_events if e.payload["event_type"] == "llm.request.started"
    )
    assert str(sse_request["payload"]["input"]).startswith("[redacted]")

    # turn.completed embeds the full final assistant message in the DURABLE trace
    # (messages store is derivable from the trace) but strips it from SSE.
    completed_row = next(row for row in rows if row["event_type"] == "turn.completed")
    assert isinstance(completed_row["payload"]["final_message"], dict)
    assert completed_row["payload"]["final_message"]["id"]
    sse_completed = next(
        e.payload for e in semantic_events if e.payload["event_type"] == "turn.completed"
    )
    assert str(sse_completed["payload"]["final_message"]).startswith("[redacted]")


def test_full_debug_trace_includes_llm_payload(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    trace_file = tmp_path / "semantic.jsonl"
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(trace_file))
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_DETAIL", "full_debug")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "show the raw prompt in full debug")

    app.state.semantic_trace_backend.flush()  # off-loop writer: drain before reading
    rows = [json.loads(line) for line in trace_file.read_text().splitlines()]
    request_row = next(row for row in rows if row["event_type"] == "llm.request.started")
    response_row = next(row for row in rows if row["event_type"] == "llm.response.completed")
    assert request_row["detail_level"] == "full_debug"
    assert request_row["payload"]["input"] == "show the raw prompt in full debug"
    assert response_row["payload"]["answer"] == "ok"


def test_metadata_detail_level_omits_payloads(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "none")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_DETAIL", "metadata")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "metadata only")

    request_event = next(
        e.payload
        for e in app.state.bus._history.get(sid, [])
        if e.type == "semantic.event" and e.payload["event_type"] == "llm.request.started"
    )
    assert request_event["detail_level"] == "metadata"
    assert request_event["actor"] == {}
    assert request_event["payload"] == {}


def test_trace_backend_factory_receives_events(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    marker = tmp_path / "factory-trace.jsonl"
    module = tmp_path / "trace_factory.py"
    module.write_text(
        f"""
import json

class Backend:
    name = "test_factory"

    def __init__(self, config):
        self.config = config

    def emit(self, event):
        with open({str(marker)!r}, "a", encoding="utf-8") as f:
            f.write(json.dumps({{"event_type": event.event_type, "config": self.config}}))
            f.write("\\n")

def build(default_root, config):
    return Backend(config)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "factory")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_FACTORY", "trace_factory:build")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_CONFIG", '{"sink": "test"}')

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "factory trace")

    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert app.state.semantic_trace_backend.name == "test_factory"
    assert rows[0]["config"] == {"sink": "test"}
    assert "turn.started" in {row["event_type"] for row in rows}


def test_semantic_event_hook_fires(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    marker = tmp_path / "semantic_events.jsonl"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "semantic_event.py").write_text(
        f"""
import json

def semantic_event(event):
    with open({str(marker)!r}, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"event_type": event["event_type"], "trace_id": event["trace_id"]}}))
        f.write("\\n")
"""
    )
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "none")
    install_global_registry(HookRegistry(hooks_dir=hooks_dir))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        client = TestClient(app)
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "hello")
    finally:
        install_global_registry(None)

    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert rows[0]["event_type"] == "turn.started"
    assert "turn.completed" in {row["event_type"] for row in rows}


def test_tool_observer_emits_semantic_tool_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "none")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("fs_read_file", {"path": "x.txt"}, "started", None)
    observer("fs_read_file", {"path": "x.txt"}, "completed", None)

    semantic = [
        e.payload for e in app.state.bus._history.get(sid, []) if e.type == "semantic.event"
    ]
    assert [e["event_type"] for e in semantic] == [
        "tool.call.started",
        "tool.call.completed",
    ]
    assert semantic[0]["actor"]["tool"] == "fs_read_file"
    assert semantic[1]["status"] == "completed"


def test_artifact_and_builtin_command_semantic_events(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "none")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_DiffAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "write a result")
    client.post(f"/v1/sessions/{sid}/commands/cache-stats")

    semantic = [
        e.payload for e in app.state.bus._history.get(sid, []) if e.type == "semantic.event"
    ]
    artifact = next(e for e in semantic if e["event_type"] == "artifact.proposed")
    command = next(e for e in semantic if e["event_type"] == "command.invocation.completed")
    assert artifact["subject"]["path"] == "result.txt"
    assert artifact["payload"]["new_content"].startswith("[redacted]")
    assert command["subject"]["command"] == "/cache-stats"


def test_lm_token_delta_projection_contract():
    """#693: the live LM-stream highway. One lm.token.delta event must feed every
    consumer correctly via the existing projections: the answer delta reaches SSE
    (live UI), while chain-of-thought is redacted to a heartbeat on SSE but kept
    verbatim on the durable/full projection (trace + ARC)."""
    from clio_agent.gact.semantic_events import (
        LM_TOKEN_DELTA,
        SemanticEvent,
        lm_token_delta_payload,
        project_full,
        project_sse,
    )

    ev = SemanticEvent(
        event_type=LM_TOKEN_DELTA,
        session_id="sess_x",
        trace_id="trace_x",
        turn_id="turn_x",
        actor={"agent_id": "synthesis", "role": "expert"},
        payload=lm_token_delta_payload(
            content="Hello ", reasoning="let me think...", field="answer"
        ),
    )

    sse = project_sse(ev)
    full = project_full(ev)

    # Envelope categorization rides every projection (by session/turn/expert).
    assert sse["session_id"] == "sess_x" and sse["turn_id"] == "turn_x"
    assert sse["actor"].get("agent_id") == "synthesis"

    # Answer delta survives to the live UI; reasoning is redacted to a heartbeat.
    assert sse["payload"]["delta"] == "Hello "
    assert sse["payload"]["field"] == "answer"
    assert sse["payload"]["reasoning"] != "let me think..."
    assert "redacted" in str(sse["payload"]["reasoning"]).lower()

    # The durable/full view (trace + ARC) keeps both verbatim.
    assert full["payload"]["delta"] == "Hello "
    assert full["payload"]["reasoning"] == "let me think..."


def test_lm_token_delta_payload_omits_empty_channels():
    from clio_agent.gact.semantic_events import lm_token_delta_payload

    assert lm_token_delta_payload(content="x") == {"field": "answer", "delta": "x"}
    assert lm_token_delta_payload(reasoning="y") == {"field": "answer", "reasoning": "y"}
