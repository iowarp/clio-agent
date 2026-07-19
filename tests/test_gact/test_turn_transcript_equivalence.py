"""Wire-equivalence tests for the TurnTranscript migration (#767 PR1).

Two layers of proof that PR1 changes nothing on the wire:

1. **Golden-trace scenarios** — three representative turns (streamed text,
   tool-call, multi-part with thinking) are driven end-to-end through the
   TestClient; the full transcript-vocabulary event stream plus the persisted
   assistant parts are normalized and compared against goldens captured on
   ``develop`` BEFORE this change (``goldens/turn_transcript_pr1/*.json``).
   To (re)capture the goldens, check out the reference tree and run::

       CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN=1 uv run --extra dev pytest \
           tests/test_gact/test_turn_transcript_equivalence.py -k golden --no-cov
2. **Shim equivalence** — the tool-observer/delegation append helpers are
   driven twice, once with no transcript open (the legacy dict path — every
   production turn in PR1) and once with a TurnTranscript open (the shimmed
   path); both must publish the identical event sequence and hold identical
   live parts.
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

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# scenario's ``build_app(agent=...)`` host fake (scenarios that monkeypatch
# ``_try_streamed_forward`` are unaffected).
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
    r"(?:msg_(?:user|asst|tool)|part|call|sess|turn|ques|att|trace|cancel)_[0-9a-f]{8,}"
)
_VOLATILE_KEY_TOKENS = (
    "duration",
    "_at",
    "cost",
    "tokens",
    "elapsed",
    "latency",
    "uptime",
    # #948 S4b: a default session now runs the default-registry blueprint react
    # ``main`` (the legacy planner that produced the original develop goldens is
    # gone), so ``message.completed`` metadata gains the dynamic-agent
    # ``agent_runtime`` provenance + its ``prompt_resolution`` block. Rather than
    # drop those whole subtrees (which would let a wire regression in the
    # provenance shape hide — the #948-S4b review finding), the goldens now COMPARE
    # that structure and exclude ONLY the genuinely environment-specific leaf: the
    # absolute ``definition_path`` values (per-run pytest tmp dirs). Everything else
    # in the provenance is deterministic via the conftest default-registry fixture
    # (blueprint id/version/scope, the ``package://`` prompt ``source_path``, the
    # prompt checksum, commands), so it locks. The goldens were regenerated on this
    # tree to capture the added provenance structure; a POSITIVE shape assertion
    # lives in ``test_default_main_turn_stamps_agent_runtime_provenance`` below.
    "definition_path",
)


class _Normalizer:
    """Rewrite volatile ids to stable tokens in order of first appearance."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def _token(self, raw: str) -> str:
        if raw not in self._map:
            prefix = raw.split("_")[0] if not raw.startswith("msg_") else "msg"
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
    parts = [normalizer.normalize(part) for m in assistants for part in m["parts"]]
    return {"events": events, "persisted_assistant_parts": parts}


def _complete_turn(client: TestClient, sid: str, text: str, *, timeout: float = 90.0) -> None:
    """Self-contained POST + settle poll (kept local so the module runs
    unchanged on the reference tree when regenerating goldens)."""

    ack = client.post(f"/v1/sessions/{sid}/messages", json={"text": text})
    assert ack.status_code == 200, ack.text
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
                    return
                break
        time.sleep(0.05)
    raise TimeoutError(f"turn for {user_id!r} did not settle")


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

        observer = current_tool_runtime().tool_observer
        assert observer is not None, "tool hooks not installed"
        observer("fs_read_file", {"path": "README.md"}, "started", None)
        observer(
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
    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory

    return ARCMemory(data_dir=str(tmp_path / name), store=_MemoryStore())


def _build(tmp_path: Path, name: str, agent: Any) -> Any:
    from clio_agent.gact.app import build_app

    return build_app(
        sessions_path=tmp_path / f"{name}.json",
        agent=agent,
        arc=_fresh_test_arc(tmp_path, f"arc_{name}"),
    )


def scenario_streamed_text_turn(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """A live-streamed plain text answer: added -> deltas -> completed."""

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("Hello ")
        await emit_chunk("streamed ")
        await emit_chunk("world.")
        return _Pred(
            answer="Hello streamed world.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
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

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("Weighing the options...", None, "provider_thinking:anthropic")
        await emit_chunk("I should answer directly. ", None, "reasoning")
        await emit_chunk("Because it is simple.", None, "reasoning")
        await emit_chunk("The answer ", None, "answer")
        await emit_chunk("is 42.", None, "answer")
        return _Pred(
            answer="The answer is 42.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
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
        f"wire behavior diverged from the develop golden for {name!r}: "
        "same turn must emit the identical SSE event sequence and persist "
        "the identical assistant parts"
    )


# ---------------------------------------------------------------------------
# golden-trace equivalence (before == after on the wire)
# ---------------------------------------------------------------------------


def test_streamed_text_turn_matches_develop_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden("streamed_text_turn", scenario_streamed_text_turn(tmp_path, monkeypatch))


def test_tool_call_turn_matches_develop_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden("tool_call_turn", scenario_tool_call_turn(tmp_path, monkeypatch))


def test_multi_part_thinking_turn_matches_develop_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden(
        "multi_part_thinking_turn", scenario_multi_part_thinking_turn(tmp_path, monkeypatch)
    )


def test_default_main_turn_stamps_agent_runtime_provenance(tmp_path: Path) -> None:
    """POSITIVE lock: a default-registry-main turn stamps the runtime provenance.

    The golden diff excludes the env-specific ``definition_path`` leaves, and the
    develop goldens historically dropped the whole ``agent_runtime`` /
    ``prompt_resolution`` subtrees — so this test is the compensating positive
    assertion (#948 S4b review): it drives a real default-main turn and asserts the
    persisted ``message.completed`` metadata carries the dynamic-agent provenance
    with the right SHAPE. A wire regression in that class (dropped block, wrong
    agent_id/execution_mode/blueprint id, or missing prompt_resolution) turns this
    red where the id-normalized golden alone could not.
    """

    from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID

    app = _build(tmp_path, "provenance", _PlainAgent("plain answer"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        _complete_turn(client, sid, "hello")
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]

    assistants = [
        m for m in messages if m["role"] == "assistant" and not m.get("metadata", {}).get("live")
    ]
    assert assistants, "no persisted assistant message"
    meta = assistants[0].get("metadata") or {}

    runtime = meta.get("agent_runtime")
    assert isinstance(runtime, dict), "agent_runtime provenance missing from the wire"
    assert runtime.get("kind") == "dynamic_agent"
    assert runtime.get("agent_id") == "main"
    assert runtime.get("execution_mode") == "blueprint_react"
    assert isinstance(runtime.get("module"), dict) and runtime["module"].get("kind") == "react"
    blueprint = runtime.get("agent_blueprint")
    assert isinstance(blueprint, dict), "agent_blueprint provenance missing"
    assert blueprint.get("id") == DEFAULT_AGENT_BLUEPRINT_ID
    assert blueprint.get("version") and blueprint.get("scope")

    prompt_res = meta.get("prompt_resolution")
    assert isinstance(prompt_res, dict), "prompt_resolution provenance missing from the wire"
    # Structural keys present (values are deterministic via the conftest fixture).
    for key in ("id", "profile", "scope", "source_path", "checksum"):
        assert prompt_res.get(key), f"prompt_resolution.{key} missing"


# ---------------------------------------------------------------------------
# shim equivalence: legacy dict path == transcript-shimmed path
# ---------------------------------------------------------------------------


def _drive_live_part_producers(app: Any, sid: str) -> None:
    """The exact call shapes the tool observer + delegation settle paths use."""

    from clio_agent.gact import tool_observer as to
    from clio_agent.gact.types import Part

    to._ensure_live_assistant_message(app, sid)
    to._append_live_assistant_part_once(
        app,
        sid,
        "route:data",
        Part(
            id="live_route_data",
            type="routing_decision",
            agent_id="main",
            selected_agent="data",
            rationale="Agent planner selected data for tool fs_read_file.",
            metadata={"route_source": "live_tool_observer", "stream_source": "live"},
            execution_path="orchestrator -> data",
        ),
    )
    # Duplicate banner: must be dropped on both paths.
    to._append_live_assistant_part_once(
        app,
        sid,
        "route:data",
        Part(id="live_route_data", type="routing_decision", agent_id="main"),
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
            "live_route_data",
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
