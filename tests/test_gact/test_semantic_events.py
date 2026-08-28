"""Research-grade semantic execution event stream."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_tool_observer, build_app
from clio_agent.gact.hooks import install_global_dispatcher
from tests._config_layer import set_config
from tests.test_gact._hook_fixtures import make_command_dispatcher

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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
    set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
    set_config("trace.path", str(trace_dir))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "analyze this")

    history = app.state.bus._history.get(sid, [])
    semantic_events = [e for e in history if e.type == "semantic.event"]
    bus_event_types = {e.payload["event_type"] for e in semantic_events}
    # WS1 serving contract: the SSE bus carries ONLY the UI atoms (+ errors); the
    # substrate/lifecycle events (turn.*, hook.*, agent.invocation.*, llm.*) are
    # routed to the durable trace + ARC, NOT the bus. So none of those leak here.
    for excluded in (
        "turn.started",
        "turn.completed",
        "hook.invocation.started",
        "agent.invocation.started",
        "llm.request.started",
        "llm.response.completed",
    ):
        assert excluded not in bus_event_types, excluded
    # clio transmits, it does not editorialize: no ui_summary/result_summary captions
    # are authored into any payload that does reach the bus.
    for e in semantic_events:
        assert "ui_summary" not in e.payload["payload"]
        assert "result_summary" not in e.payload["payload"]

    app.state.semantic_trace_backend.flush()  # off-loop writer: drain before reading
    trace_path = trace_dir / f"{sid}.semantic.jsonl"
    rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    # The DURABLE trace stays FULL: the whole lifecycle sequence is captured there
    # (only the bus projection narrows). ARC's op-logger also mirrors each ARC write
    # as ``arc.op`` rows; filter them out to assert the leading SEMANTIC sequence.
    semantic_rows = [row for row in rows if row["event_type"] != "arc.op"]
    trace_event_types = [row["event_type"] for row in semantic_rows]
    for required in (
        "turn.started",
        "hook.invocation.started",
        "agent.invocation.started",
        "llm.request.started",
        "llm.response.completed",
        "agent.invocation.completed",
        "turn.completed",
    ):
        assert required in trace_event_types, required
    # Session lifecycle is now a first-class semantic event with its own stable
    # trace. Turn-scoped rows retain the original message-derived trace contract.
    turn_rows = [row for row in semantic_rows if not row["event_type"].startswith("session.")]
    assert all(row["trace_id"].startswith("trace_msg_user_") for row in turn_rows)
    # The DURABLE canonical trace captures FULL (unredacted) bodies.
    request_row = next(row for row in rows if row["event_type"] == "llm.request.started")
    assert request_row["payload"]["input"] == "analyze this"
    # turn.completed's durable payload under atoms (the only regime since v0.8.0):
    # the final_message byte-copy is DELETED because the message_part atoms carry
    # the wire identity (design §4.2 step 5 — "kill final_message"); it survives
    # only in the no-ARC structural case.
    from clio_agent.gact.transcript_projection import atoms_active

    completed_row = next(row for row in rows if row["event_type"] == "turn.completed")
    if atoms_active(app):
        assert "final_message" not in completed_row["payload"]
    else:
        assert isinstance(completed_row["payload"]["final_message"], dict)
        assert completed_row["payload"]["final_message"]["id"]
        assert "[redacted]" not in str(completed_row["payload"]["final_message"])


def test_full_debug_trace_includes_llm_payload(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    trace_file = tmp_path / "semantic.jsonl"
    set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
    set_config("trace.path", str(trace_file))
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_DETAIL", "full_debug")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    # Keep one application lifespan around the background turn. Constructing a
    # TestClient without entering it creates a fresh portal around each request;
    # the slower 3.13 CI runner could therefore tear down the turn portal after
    # the assistant message became readable but before the response trace reached
    # the shared writer. Production keeps one lifespan for the whole server, and
    # this test must exercise that same ordering contract.
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "show the raw prompt in full debug")
        app.state.semantic_trace_backend.flush()  # drain before leaving the lifespan

    rows = [json.loads(line) for line in trace_file.read_text().splitlines()]
    request_row = next(row for row in rows if row["event_type"] == "llm.request.started")
    response_row = next(row for row in rows if row["event_type"] == "llm.response.completed")
    assert request_row["detail_level"] == "full_debug"
    assert request_row["payload"]["input"] == "show the raw prompt in full debug"
    assert response_row["payload"]["answer"] == "ok"


def test_metadata_detail_level_omits_payloads() -> None:
    """At ``metadata`` detail the SSE projection keeps the envelope but drops the
    body (payload/actor/subject); the durable ``full`` projection keeps everything."""
    from clio_agent.gact.semantic_events import SemanticEvent, project_full, project_sse

    ev = SemanticEvent(
        event_type="llm.request.started",
        session_id="sess_x",
        trace_id="trace_x",
        turn_id="turn_x",
        detail_level="metadata",
        actor={"agent_id": "data"},
        payload={"input": "secret prompt body"},
    )
    sse = project_sse(ev)
    full = project_full(ev)

    assert sse["detail_level"] == "metadata"
    assert sse["actor"] == {}
    assert sse["payload"] == {}
    # The durable view keeps the full body regardless of the SSE detail level.
    assert full["payload"]["input"] == "secret prompt body"
    assert full["actor"] == {"agent_id": "data"}


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
    set_config("trace.backend", "factory")  # file-layer (file > env); #985 config-first
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
    # A real SemanticEvent subprocess hook (the ported ``semantic_event`` consumer):
    # reads the wire envelope from stdin and appends the projected event's type/trace.
    body = f"""
import json, sys

envelope = json.load(sys.stdin)
payload = envelope["payload"]
with open({str(marker)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"event_type": payload["event_type"], "trace_id": payload["trace_id"]}}))
    f.write("\\n")
"""
    set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
    install_global_dispatcher(make_command_dispatcher(tmp_path, event="SemanticEvent", body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        client = TestClient(app)
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "hello")
    finally:
        install_global_dispatcher(None)

    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    # The GLOBAL dispatcher captures every semantic event in the process, so scope
    # the ordering assertion to THIS turn's trace: a stray event from another
    # session (e.g. a background LM failure elsewhere in the suite) must not be
    # able to claim rows[0]. The hook records trace_id for exactly this purpose.
    assert rows, "the SemanticEvent hook never fired"
    turn_trace = next(row["trace_id"] for row in rows if row["event_type"] == "turn.started")
    trace_rows = [row for row in rows if row["trace_id"] == turn_trace]
    assert trace_rows[0]["event_type"] == "turn.started"
    assert "turn.completed" in {row["event_type"] for row in trace_rows}


def test_tool_observer_emits_semantic_tool_events(tmp_path: Path, monkeypatch) -> None:
    set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("fs_read_file", {"path": "x.txt"}, "started", None)
    observer("fs_read_file", {"path": "x.txt"}, "completed", None)

    history = app.state.bus._history.get(sid, [])
    # WS1: ONE tool representation on the bus -- the lean DEDICATED tool.call.* events
    # (carrying call_id/tool/args + ok/duration_ms/cached). The redundant
    # ``semantic.event`` tool mirror is routed to the trace/ARC only, NOT the bus.
    dedicated = [e for e in history if e.type in ("tool.call.started", "tool.call.completed")]
    assert [e.type for e in dedicated] == ["tool.call.started", "tool.call.completed"]
    assert dedicated[0].payload["tool"] == "fs_read_file"
    assert dedicated[1].payload["ok"] is True
    # No ui_summary/result_summary captions on the served tool payload.
    assert "ui_summary" not in dedicated[1].payload
    assert "result_summary" not in dedicated[1].payload
    # The semantic.event mirror of the tool call does NOT leak onto the bus.
    tool_mirror = [
        e
        for e in history
        if e.type == "semantic.event"
        and e.payload["event_type"] in ("tool.call.started", "tool.call.completed")
    ]
    assert tool_mirror == []


def test_tool_observer_stamps_curated_title_on_call_events(tmp_path: Path) -> None:
    """Round-9 wire defect: obs "called" rows rendered the raw tool name because
    the tool.call.started/completed events never carried the title the observer
    already resolves (and already stamps on the tool_call Part). The dedicated
    bus events must carry it too, for a curated tool."""

    from clio_agent.gact.agents.tool_instrumentation import instrument_tools, native_tool

    def _rank_stations(**_: object) -> str:
        return "ranked"

    tool = native_tool(
        _rank_stations,
        name="p5_rank_stations",
        desc="rank",
        args={},
        title="Rank stations",
    )
    instrument_tools([tool])

    set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("p5_rank_stations", {}, "started", None)
    observer("p5_rank_stations", {}, "completed", None, "ranked")

    history = app.state.bus._history.get(sid, [])
    dedicated = {
        e.type: e for e in history if e.type in ("tool.call.started", "tool.call.completed")
    }
    assert dedicated["tool.call.started"].payload["tool_title"] == "Rank stations"
    assert dedicated["tool.call.completed"].payload["tool_title"] == "Rank stations"


def test_tool_observer_omits_title_when_tool_uncurated(tmp_path: Path) -> None:
    """An uncurated tool's call events carry no tool_title -- never fabricated,
    matching Part.tool_title's own "empty when uncurated" contract."""

    set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("fs_read_file", {"path": "x.txt"}, "started", None)
    observer("fs_read_file", {"path": "x.txt"}, "completed", None)

    history = app.state.bus._history.get(sid, [])
    dedicated = {
        e.type: e for e in history if e.type in ("tool.call.started", "tool.call.completed")
    }
    assert "tool_title" not in dedicated["tool.call.started"].payload
    assert "tool_title" not in dedicated["tool.call.completed"].payload


def test_artifact_and_builtin_command_semantic_events(tmp_path: Path, monkeypatch) -> None:
    from .conftest import complete_turn

    set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
    app = build_app(sessions_path=tmp_path / "s.json", agent=_DiffAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "write a result")
        client.post(f"/v1/sessions/{sid}/commands/cache-stats")

        history = app.state.bus._history.get(sid, [])
        # WS1: the artifact reaches the UI as a real ``file_diff`` PART (not a redundant
        # semantic.event), carrying the proposed content unredacted so the UI renders the
        # diff; the ``artifact.proposed`` semantic mirror is routed to trace/ARC only.
        diff_parts = [
            e.payload["part"]
            for e in history
            if e.type == "message.part.added"
            and e.payload.get("part", {}).get("type") == "file_diff"
        ]
        assert any(p.get("path") == "result.txt" for p in diff_parts)
        assert "[redacted]" not in str(diff_parts)
        # WS1: the built-in command result reaches the UI as a real assistant
        # ``message.created`` (so the TUI shows it); the ``command.invocation.completed``
        # semantic mirror is trace-only, NOT on the bus.
        command_msgs = [
            e.payload
            for e in history
            if e.type == "message.created"
            and e.payload.get("metadata", {}).get("synthetic") == "command_result"
        ]
        assert command_msgs, "command result must reach the UI as an assistant message"
        assert any(
            "cache-stats" in str(m.get("metadata", {}).get("command", "")) for m in command_msgs
        )
        bus_semantic_types = {
            e.payload["event_type"] for e in history if e.type == "semantic.event"
        }
        assert "artifact.proposed" not in bus_semantic_types
        assert "command.invocation.completed" not in bus_semantic_types


def test_lm_token_delta_projection_contract():
    """#693: the live LM-stream highway. One lm.token.delta event must feed every
    consumer correctly via the existing projections: both the answer delta AND the
    chain-of-thought reach SSE (live UI) unredacted — CLIO does not hide the user's
    own reasoning — and are kept verbatim on the durable/full projection (trace + ARC)."""
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

    # Both the answer delta AND the reasoning survive to the live UI unredacted.
    assert sse["payload"]["delta"] == "Hello "
    assert sse["payload"]["field"] == "answer"
    assert sse["payload"]["reasoning"] == "let me think..."

    # The durable/full view (trace + ARC) keeps both verbatim.
    assert full["payload"]["delta"] == "Hello "
    assert full["payload"]["reasoning"] == "let me think..."


def test_lm_token_delta_payload_omits_empty_channels():
    from clio_agent.gact.semantic_events import lm_token_delta_payload

    assert lm_token_delta_payload(content="x") == {"field": "answer", "delta": "x"}
    assert lm_token_delta_payload(reasoning="y") == {"field": "answer", "reasoning": "y"}
