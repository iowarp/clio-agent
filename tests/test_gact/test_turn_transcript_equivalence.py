"""Recorded transcript traces and confirmed model-input ownership.

Streamed text, tool calls and thinking retain strict normalized event/part
baselines. R2 intentionally changes submission to HTTP 202 plus an assignment
handle and moves input acknowledgement to Core. Baseline changes must be
inspected; regeneration alone is not acceptance.

Producer-equivalence cases separately compare passive live views with and
without an open TurnTranscript. Both views preserve the observed parts.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests._harness import emit_live_text, install_scripted_module

# #948 S4b: default sessions run their pinned plugin agent; route it to each
# scenario's ``build_app(agent=...)`` host fake (the streamed scenarios install
# their own scripted module, which streams through the LM token hooks).
pytestmark = pytest.mark.usefixtures("host_agent_executor")

GOLDEN_DIR = Path(__file__).parent / "goldens" / "turn_transcript_pr1"
_GOLDEN_REGEN = os.environ.get("CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN") == "1"

# The transcript wire vocabulary under equivalence. #767 PR5 retired the
# normalized turn.text.delta / turn.trace.delta / turn.action.added /
# call.result.delta twins (they had zero consumers), so they are no longer
# captured — message.part.* (+ tool.call.* telemetry) is the surviving wire.
_EVENT_TYPES_UNDER_TEST = (
    "message.created",
    "message.part.added",
    "message.part.delta",
    "message.part.completed",
    "message.completed",
    "tool.call.started",
    "tool.call.completed",
)

# No \b anchors: generated ids embed inside composite ids (live_call_<hex>_result)
# and "_" is a word character, so boundaries would skip exactly those.
_ID_PATTERN = re.compile(
    r"(?:msg_(?:user|asst|tool)|part|call|sess|turn|ques|att|trace|cancel|task)_[0-9a-f]{8,}"
    r"|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)
_VOLATILE_KEY_TOKENS = (
    "duration",
    "_at",
    "cost",
    "tokens",
    "elapsed",
    "latency",
    "uptime",
    # Absolute per-run paths in the agent provenance (pytest tmp dirs).
    "definition_path",
)


class _Normalizer:
    """Rewrite volatile ids to stable tokens in order of first appearance."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def _token(self, raw: str) -> str:
        if raw not in self._map:
            prefix = "core" if "-" in raw else raw.split("_")[0]
            if raw.startswith("msg_"):
                prefix = "msg"
            self._map[raw] = f"<{prefix}#{len(self._map) + 1}>"
        return self._map[raw]

    def normalize(self, value: Any) -> Any:
        if isinstance(value, str):
            return _ID_PATTERN.sub(lambda m: self._token(m.group(0)), value)
        if isinstance(value, dict):
            return {
                key: self.normalize(val)
                for key, val in value.items()
                if not any(token in key for token in _VOLATILE_KEY_TOKENS)
            }
        if isinstance(value, list):
            return [self.normalize(item) for item in value]
        return value


def _normalized_trace(app: Any, client: TestClient, sid: str) -> dict[str, Any]:
    """The scenario's observable surface: SSE-visible events + persisted parts."""

    normalizer = _Normalizer()
    events = [
        {"type": event.type, "payload": normalizer.normalize(event.payload)}
        for event in app.state.bus._history.get(sid, [])
        if event.type in _EVENT_TYPES_UNDER_TEST
    ]
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert len(assistants) == 1, "accepted input must not wake a duplicate model segment"
    human = next(message for message in messages if message["role"] == "user")
    history = app.state.task_history
    committed = history.committed_notification_input(sid, human["id"])
    assert committed is not None, "the model input must be confirmed in Core"
    assert human["id"] in history.acknowledged_notifications(sid)
    assert committed.session_id == sid and committed.human_input
    assert committed.turn_id == assistants[0]["turn_id"]
    notice = history.mail_item(sid, human["id"])
    assert notice is not None
    assert committed.content[committed.notification_ids.index(notice.id)] == notice.content
    parts = [normalizer.normalize(part) for m in assistants for part in m["parts"]]
    return {"events": events, "persisted_assistant_parts": parts}


def _complete_turn(client: TestClient, sid: str, text: str, *, timeout: float = 90.0) -> None:
    """Self-contained POST + settle poll (kept local so the module runs
    unchanged on the reference tree when regenerating goldens)."""

    ack = client.post(f"/v1/sessions/{sid}/messages", json={"text": text})
    assert ack.status_code == 202, ack.text
    assert ack.json()["task_handle"].startswith("task_")
    user_id = ack.json()["message_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        for i, m in enumerate(msgs):
            if m.get("id") == user_id:
                if (
                    i > 0
                    and msgs[i - 1]["role"] == "assistant"
                    # The live in-flight projection also shows up here; only a
                    # PERSISTED assistant settles the turn (else the trace is
                    # captured mid-turn and the golden becomes racy).
                    and not msgs[i - 1].get("metadata", {}).get("live")
                ):
                    _wait_until_not_running(client, sid, deadline)
                    return
                break
        time.sleep(0.05)
    raise TimeoutError(f"turn for {user_id!r} did not settle")


def _wait_until_not_running(client: TestClient, sid: str, deadline: float) -> None:
    """Wait for the session to leave ``running`` (the whole turn is on the bus).

    The assistant message is persisted BEFORE ``message.completed`` publishes (#1469),
    so a ledger hit alone let the trace be captured without ``message.completed`` (the
    divergence this test hit under ``-n 2`` load). The status leaves ``running`` only
    once the turn released its slot (#1466), after every event the trace compares.
    A status read, not a bus API, so the module still runs on the reference tree.
    """

    while time.monotonic() < deadline:
        if client.get(f"/v1/sessions/{sid}").json()["status"] != "running":
            return
        time.sleep(0.05)
    raise TimeoutError(f"session {sid!r} never left running")


# ---------------------------------------------------------------------------
# scenario harness (importable by the --regen entry point)
# ---------------------------------------------------------------------------


class _Pred:
    def __init__(self, **fields: Any) -> None:
        self.answer = ""
        self.selected_expert = ""
        self.routing_rationale = ""
        self.route_source = ""
        self.route_reason = ""
        self.error_info = None
        for key, value in fields.items():
            setattr(self, key, value)


class _PlainAgent:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    def forward(self, question: str, session_id: str) -> Any:
        return _Pred(
            answer=self._answer,
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )


class _ToolCallingAgent(_PlainAgent):
    def forward(self, question: str, session_id: str) -> Any:
        from clio_agent.tools.execution import current_tool_runtime
        from clio_agent.tools.tool_observation import notify_tool_observer

        observer = current_tool_runtime().tool_observer
        assert observer is not None, "tool hooks not installed"
        notify_tool_observer(observer, "fs_read_file", {"path": "README.md"}, "started", None)
        notify_tool_observer(
            observer,
            "fs_read_file",
            {"path": "README.md"},
            "completed",
            None,
            result={"ok": True, "text": "readme contents"},
        )
        return _Pred(
            answer="TOOL_TURN_DONE",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )


def _fresh_test_arc(tmp_path: Path, name: str) -> Any:
    from clio_agent.arc.memory import ARCMemory

    # Acceptance and recipient-input receipts require the real confirmed Core writer.
    # The test namespace fixture isolates and clears this ordinary ARC instance.
    return ARCMemory(data_dir=str(tmp_path / name))


def _build(tmp_path: Path, name: str, agent: Any) -> Any:
    from clio_agent.gact.app import build_app

    return build_app(
        sessions_path=tmp_path / f"{name}.json",
        agent=agent,
        arc=_fresh_test_arc(tmp_path, f"arc_{name}"),
    )


def scenario_streamed_text_turn(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """A live-streamed plain text answer: added -> deltas -> completed."""

    def streamed_forward(**kwargs: Any) -> Any:
        del kwargs
        emit_live_text("Hello ")
        emit_live_text("streamed ")
        emit_live_text("world.")
        return _Pred(
            answer="Hello streamed world.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    install_scripted_module(monkeypatch, streamed_forward)
    app = _build(tmp_path, "streamed", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        _complete_turn(client, sid, "stream a text answer")
        return _normalized_trace(app, client, sid)


def scenario_tool_call_turn(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """A turn whose forward triggers the live tool observer (call + result)."""

    del monkeypatch
    app = _build(tmp_path, "toolcall", _ToolCallingAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _complete_turn(client, sid, "call a tool please")
        trace = _normalized_trace(app, client, sid)
    # The scenario is only meaningful if the observer really fired.
    assert any(e["type"] == "tool.call.started" for e in trace["events"])
    assert any(e["type"] == "tool.call.completed" for e in trace["events"])
    return trace


def scenario_multi_part_thinking_turn(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """Provider thinking + reasoning + answer: three live parts, split by field."""

    def streamed_forward(**kwargs: Any) -> Any:
        del kwargs
        emit_live_text("Weighing the options...", "", "provider_thinking:anthropic")
        emit_live_text("I should answer directly. ", "", "reasoning")
        emit_live_text("Because it is simple.", "", "reasoning")
        emit_live_text("The answer ", "", "answer")
        emit_live_text("is 42.", "", "answer")
        return _Pred(
            answer="The answer is 42.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    install_scripted_module(monkeypatch, streamed_forward)
    app = _build(tmp_path, "thinking", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "th"}).json()["id"]
        _complete_turn(client, sid, "think then answer")
        return _normalized_trace(app, client, sid)


def _assert_matches_golden(name: str, trace: dict[str, Any]) -> None:
    golden_path = GOLDEN_DIR / f"{name}.json"
    if _GOLDEN_REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert golden_path.exists(), (
        f"golden {golden_path} missing — regenerate on the reference tree with "
        "CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN=1 (see module docstring)"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert trace == golden, (
        f"wire behavior diverged from the recorded baseline for {name!r}: "
        "same turn must emit the identical SSE event sequence and persist "
        "the identical assistant parts"
    )


# ---------------------------------------------------------------------------
# golden-trace equivalence (before == after on the wire)
# ---------------------------------------------------------------------------


def test_streamed_text_turn_matches_recorded_wire_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden("streamed_text_turn", scenario_streamed_text_turn(tmp_path, monkeypatch))


def test_tool_call_turn_matches_recorded_wire_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden("tool_call_turn", scenario_tool_call_turn(tmp_path, monkeypatch))


def test_multi_part_thinking_turn_matches_recorded_wire_golden(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _assert_matches_golden(
        "multi_part_thinking_turn", scenario_multi_part_thinking_turn(tmp_path, monkeypatch)
    )


def test_default_turn_stamps_plugin_agent_provenance(tmp_path: Path) -> None:
    """POSITIVE lock: a default session's turn stamps its pinned plugin agent's provenance.

    The golden diff excludes environment-specific leaves, so this test asserts the
    persisted ``message.completed`` metadata carries the agent runtime provenance:
    the session's agent (``clio/standard`` from the test marketplace), its flow, the
    pinned resolution digest and its plugin set.
    """

    app = _build(tmp_path, "provenance", _PlainAgent("plain answer"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        _complete_turn(client, sid, "hello")
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        pinned = client.get(f"/v1/sessions/{sid}/agent").json()["resolution"]["digest"]

    assistants = [
        m for m in messages if m["role"] == "assistant" and not m.get("metadata", {}).get("live")
    ]
    assert assistants, "no persisted assistant message"
    runtime = (assistants[0].get("metadata") or {}).get("agent_runtime")
    assert isinstance(runtime, dict), "agent_runtime provenance missing from the wire"
    assert runtime.get("kind") == "dynamic_agent"
    assert runtime.get("agent_id") == "standard"
    assert runtime.get("execution_mode") == "flow_react"
    assert runtime.get("source") == "plugin"
    assert runtime.get("tools"), "the standard agent carries its resolved tools"
    resolution = runtime.get("agent_resolution")
    assert isinstance(resolution, dict), "agent_resolution provenance missing"
    assert resolution["ref"] == "clio/standard"
    assert resolution["digest"] == pinned
    assert "core" in resolution["plugins"] and resolution["flow"]["id"] == "react"


# ---------------------------------------------------------------------------
# shim equivalence: legacy dict path == transcript-shimmed path
# ---------------------------------------------------------------------------


def _drive_live_part_producers(app: Any, sid: str) -> None:
    """The exact call shapes the tool observer + delegation settle paths use."""

    from clio_agent.gact import tool_observer as to
    from clio_agent.gact.types import Part

    to._ensure_live_assistant_message(app, sid)
    # The once-key producer shape src still uses: the live tool observer's
    # handoff-context part (routing decisions became semantic events with
    # a0e1d9a9, so no routing_decision part rides the wire any more).
    to._append_live_assistant_part_once(
        app,
        sid,
        "handoff:data:csv_expert",
        Part(
            id="live_handoff_data_csv_expert",
            type="expert_handoff",
            agent_id="data",
            parent_agent="data",
            child_agent="csv_expert",
            stage="tool.started",
            status="running",
            text="data -> csv_expert",
            metadata={"stream_source": "live", "route_source": "live_tool_observer"},
        ),
    )
    # Duplicate once-key append: must be dropped on both paths.
    to._append_live_assistant_part_once(
        app,
        sid,
        "handoff:data:csv_expert",
        Part(id="live_handoff_data_csv_expert", type="expert_handoff", agent_id="data"),
    )
    to._append_live_assistant_part(
        app,
        sid,
        Part(
            id="live_call_1_call",
            type="tool_call",
            agent_id="data",
            call_id="call_1",
            tool_name="fs_read_file",
            input={"path": "README.md"},
            metadata={"stream_source": "live", "telemetry_source": "live_observer"},
        ),
    )
    to._append_live_assistant_part(
        app,
        sid,
        Part(
            id="live_call_1_result",
            type="tool_result",
            agent_id="data",
            call_id="call_1",
            tool_name="fs_read_file",
            content=[Part(id="live_call_1_result_text", type="text", agent_id="data", text="ok")],
            metadata={"stream_source": "live", "telemetry_source": "live_observer"},
        ),
    )
    to._append_live_assistant_part(
        app,
        sid,
        Part(
            id="live_handoff_a",
            type="expert_handoff",
            agent_id="main",
            parent_agent="main",
            child_agent="data",
            stage="delegate.started",
            status="running",
            text="main -> data",
            metadata={"stream_source": "live"},
        ),
    )


def _shim_trace(app: Any, sid: str) -> dict[str, Any]:
    normalizer = _Normalizer()
    events = [
        {"type": event.type, "payload": normalizer.normalize(event.payload)}
        for event in app.state.bus._history.get(sid, [])
        if event.type in _EVENT_TYPES_UNDER_TEST
    ]
    live_parts = [
        normalizer.normalize(part.to_wire()) for part in app.state.live_assistant_parts.get(sid, [])
    ]
    return {"events": events, "live_parts": live_parts}


def test_shimmed_producers_emit_identical_stream_as_legacy(tmp_path: Path) -> None:
    """Same producer calls; one session on the legacy dict path, one with an
    open TurnTranscript. Event streams and live parts must be identical."""

    from clio_agent.gact.transcript import EventBusTranscriptPublisher

    app = _build(tmp_path, "shim", _PlainAgent("unused"))
    with TestClient(app):
        # Hex-shaped so the id normalizer maps both to the same stable token.
        legacy_sid = "sess_aaaaaaaaaaaa"
        shimmed_sid = "sess_bbbbbbbbbbbb"

        _drive_live_part_producers(app, legacy_sid)

        transcript = app.state.turn_transcripts.open_turn(
            shimmed_sid,
            "",  # legacy path stamps turn_id from the (empty) off-turn context
            EventBusTranscriptPublisher(app.state.bus, shimmed_sid),
        )
        _drive_live_part_producers(app, shimmed_sid)

        legacy = _shim_trace(app, legacy_sid)
        shimmed = _shim_trace(app, shimmed_sid)
        assert shimmed == legacy

        # The migration aliases hold: the legacy dicts mirror the ledger.
        assert app.state.live_assistant_parts[shimmed_sid] is transcript.live_parts_alias()
        assert app.state.live_assistant_message_ids[shimmed_sid] == transcript.message_id
        assert [p.id for p in transcript.snapshot()] == [
            "live_handoff_data_csv_expert",
            "live_call_1_call",
            "live_call_1_result",
            "live_handoff_a",
        ]
        app.state.turn_transcripts.close(shimmed_sid)


def test_turn_loop_settles_the_transcript_registry_after_a_turn(tmp_path: Path) -> None:
    """PR2: the turn loop opens the ledger at turn start and settles it on
    every exit path — a completed turn always leaves the registry empty
    (mid-turn open/settle behavior is covered in test_turn_transcript_pr2)."""

    app = _build(tmp_path, "noturn", _PlainAgent("plain answer"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "n"}).json()["id"]
        _complete_turn(client, sid, "hello")
        assert app.state.turn_transcripts.get(sid) is None


def test_transcript_registry_closes_before_persist_can_release_the_gil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1339 round 5: the test above is timing-sensitive -- looped 20x locally on
    this 3.12 machine, the PRE-fix ordering (registry closed AFTER
    ``persist_finalized_message``) failed 3/20 with
    ``assert <TurnTranscript object> is None`` (CI hit the same assertion on
    py3.13, 3.12 green, one of the three transient failures under contention on
    #1338's local suite). Mechanism: ``persist_finalized_message``'s
    ``_append_session_message`` call (which makes the assistant message visible
    via GET, #1334 append-first-so-a-failed-mint-never-loses-it) runs BEFORE its
    own ``minter.mint_remainder`` ARC RPC -- a real call that can release the GIL
    for wall-clock time -- so a foreign thread reacting to the now-visible
    message could still find ``app.state.turn_transcripts.get(sid)`` non-``None``
    if the registry closed only afterward (the old order, `settle_turn_transcript`
    called once at the very end).

    This test pins the ordering DETERMINISTICALLY (no GIL-timing luck needed): a
    monkeypatched ``persist_finalized_message`` RECORDS whether the registry was
    already closed at call time, rather than asserting in place -- raising inside
    it would be swallowed by ``run_finalize_or_settle_prologue_gap``'s own
    finalize-crash envelope (turning the violation into a settled error turn
    instead of a test failure) and mask the very race under test. The recorded
    observation is asserted at the top level, outside the turn's error handling.
    Reverting ``finalize_turn``'s ``state.app.state.turn_transcripts.close(
    state.sid)`` line (moving it back to run only via the later
    ``settle_turn_transcript(state)`` call) makes this fail on every run, not
    just 3/20.
    """

    import clio_agent.gact.turn_finalize as turn_finalize_module

    observed: list[bool] = []
    real_persist = turn_finalize_module.persist_finalized_message

    def _record_registry_state_then_persist(
        app: Any, session_id: str, message: Any, **kwargs: Any
    ) -> bool:
        observed.append(app.state.turn_transcripts.get(session_id) is None)
        return real_persist(app, session_id, message, **kwargs)

    monkeypatch.setattr(
        turn_finalize_module, "persist_finalized_message", _record_registry_state_then_persist
    )

    app = _build(tmp_path, "noturn2", _PlainAgent("plain answer"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "n2"}).json()["id"]
        _complete_turn(client, sid, "hello")
        assert observed == [True], (
            "persist_finalized_message must observe the transcript registry "
            f"ALREADY closed every time it runs; got {observed!r}"
        )
        assert app.state.turn_transcripts.get(sid) is None
