"""CLIO-BBBBBBBBBB19: text parts stream via message.part.delta events."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _build_prompt_user_agent_module,
    _build_stream_listeners,
    _dynamic_agent_lm_config,
    _pop_stream_fallback,
    _record_stream_fallback,
    _stream_fallback_reason_capabilities,
    _StreamingOutputError,
    _try_streamed_forward,
    build_app,
)
from clio_agent.gact.types import AgentDef


@dataclass
class _Pred:
    answer: str = ""
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def __init__(self, answer):
        self._answer = answer

    def forward(self, question: str, session_id: str):
        return _Pred(answer=self._answer)


class _DspyAgent(dspy.Module):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self._answer = answer
        self.calls: list[tuple[str, str]] = []

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del session_mode, session_edit_mode
        self.calls.append((question, session_id))
        return _Pred(answer=self._answer)


class _ExpertStreamingAgent(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.chat_agent = object()
        self.answer_synthesizer = object()

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del question, session_id, session_mode, session_edit_mode
        return _Pred(answer="sync fallback should not run")


class _FakeStreamListener:
    def __init__(self, signature_field_name: str, predict: Any) -> None:
        self.signature_field_name = signature_field_name
        self.predict = predict


@pytest.fixture()
def app_client(tmp_path: Path):
    answer = "X" * 200  # 200 chars -> 4 chunks at 64-char window.
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(answer))
    return app, TestClient(app), answer


def _assert_structured_stream_fallback(payload: dict[str, Any], reason: str) -> None:
    fallback = payload["stream_fallback"]
    assert fallback["reason"] == reason
    assert fallback["synthetic_posthoc"] is True
    assert fallback["live_streaming"] is False
    assert isinstance(fallback["category"], str)
    assert fallback["category"]
    assert isinstance(fallback["description"], str)
    assert fallback["description"]
    assert isinstance(fallback["recovery_actions"], list)
    assert fallback["recovery_actions"]


def test_batch_text_is_delivered_without_deltas(app_client) -> None:
    app, client, answer = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    added = [
        e for e in history if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
    ]
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed = [e for e in history if e.type == "message.part.completed"]
    message_completed = [e for e in history if e.type == "message.completed"]

    # Post-hoc text arrives as a completed part rather than synthetic
    # chunks. Only real live provider output should use delta events.
    assert len(added) == 1
    assert added[0].payload["part"]["text"] == answer
    assert added[0].payload["part"]["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(added[0].payload["part"]["metadata"], "agent_not_streamable")
    assert deltas == []
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "batch"
    _assert_structured_stream_fallback(completed[0].payload, "agent_not_streamable")
    assert completed[0].payload["final_text"] == answer
    _assert_structured_stream_fallback(
        message_completed[-1].payload["metadata"], "agent_not_streamable"
    )
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
    assert text_parts[-1]["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(text_parts[-1]["metadata"], "agent_not_streamable")


def test_stream_fallback_reasons_are_audited_and_reject_unknowns(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_DspyAgent("fallback"))
    catalog = _stream_fallback_reason_capabilities()

    assert {
        "stream_disabled_guided_output",
        "stream_disabled_live_streaming",
        "streaming_dependency_unavailable",
        "agent_not_available",
        "agent_not_streamable",
        "stream_setup_failed",
        "stream_failed_before_output",
        "stream_no_prediction",
        "stream_completed_without_chunks",
        "provider_streaming_unsupported",
        "sync_execution_path",
        "dynamic_prompt_stream_unavailable",
        "dynamic_tool_stream_unavailable",
    } == set(catalog)
    for reason, details in catalog.items():
        assert details["synthetic_posthoc"] is True, reason
        assert details["live_streaming"] is False, reason
        assert details["category"], reason
        assert details["description"], reason
        assert details["recovery_actions"], reason

    with pytest.raises(ValueError, match="Unknown stream fallback reason"):
        _record_stream_fallback(app, "sid", "unclassified_silent_downgrade")


def test_sync_execution_default_fallback_is_structured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def returns_none_without_reason(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", returns_none_without_reason)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("sync answer"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed_messages = [e for e in history if e.type == "message.completed"]

    assert deltas == []
    _assert_structured_stream_fallback(
        completed_messages[-1].payload["metadata"], "sync_execution_path"
    )


async def test_streamify_setup_failure_returns_none_for_sync_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise ValueError(
            "Signature field answer is not unique in the program, cannot "
            "automatically determine which predictor to use for streaming."
        )

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    agent = _DspyAgent("fallback answer")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    result = await _try_streamed_forward(app, "stream setup fails", "sid", emit_chunk)

    assert result is None
    assert chunks == []
    assert agent.calls == []
    fallback = _pop_stream_fallback(app, "sid")
    assert fallback["reason"] == "stream_setup_failed"
    assert fallback["synthetic_posthoc"] is True
    assert fallback["live_streaming"] is False
    assert fallback["recovery_actions"]
    assert "ValueError" in fallback["message"]


async def test_batch_transport_without_live_streaming_skips_streamify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("streamify should not be called for non-streaming providers")

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    agent = _DspyAgent("sync answer")
    agent._provider_config = SimpleNamespace(provider="claude_code", claude_code_transport="exec")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    result = await _try_streamed_forward(app, "visualize", "sid", emit_chunk)

    assert result is None
    assert chunks == []
    assert agent.calls == []
    fallback = _pop_stream_fallback(app, "sid")
    assert fallback["reason"] == "provider_streaming_unsupported"
    assert fallback["synthetic_posthoc"] is True
    assert fallback["live_streaming"] is False


def test_argonne_streaming_is_not_force_classified_as_batch() -> None:
    """iowarp/clio-agent#160: ALCF (Sophia + Metis) is a plain OpenAI-compatible
    SSE endpoint that streams at the provider AND through LiteLLM (verified with a
    live multi-chunk probe). CLIO must NOT force-classify it as batch -- doing so
    bypassed the streamify pump for every ALCF run. Only the CLI-backed custom
    transports (codex JSON-RPC, claude_code exec) stay genuinely non-streaming."""

    from clio_agent.gact.app import _agent_streaming_unsupported_reason

    def _agent(provider: str) -> SimpleNamespace:
        return SimpleNamespace(_provider_config=SimpleNamespace(provider=provider))

    # Argonne (bare kind + both preset ids) must now attempt streaming.
    for provider in ("argonne", "argonne_metis", "argonne_sophia"):
        assert _agent_streaming_unsupported_reason(_agent(provider)) == "", provider

    # Claude Code SDK is streaming-capable; the explicit exec transport is not.
    assert _agent_streaming_unsupported_reason(_agent("claude_code")) == ""
    assert (
        _agent_streaming_unsupported_reason(
            SimpleNamespace(
                _provider_config=SimpleNamespace(
                    provider="claude_code",
                    claude_code_transport="sdk",
                )
            )
        )
        == ""
    )
    assert (
        _agent_streaming_unsupported_reason(
            SimpleNamespace(
                _provider_config=SimpleNamespace(
                    provider="claude_code",
                    claude_code_transport="exec",
                )
            )
        )
        == "provider_streaming_unsupported"
    )

    # Codex JSON-RPC remains force-classified as batch.
    assert _agent_streaming_unsupported_reason(_agent("codex")) == "provider_streaming_unsupported"


@pytest.mark.asyncio
async def test_dynamic_agent_module_carries_non_streaming_provider_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("streamify should not be called for Codex dynamic agents")

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    from clio_agent.config import LMProviderConfig

    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(
            provider="codex",
            api_base="codex://exec",
            model="gpt-5.5",
            api_key="x",
            codex_transport="exec",
        )
    )
    module = _build_prompt_user_agent_module(
        base_agent,
        AgentDef(
            id="reference",
            source="expert_pack",
            title="Reference Expert",
            system_prompt="Review reference evidence.",
        ),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=base_agent)

    result = await _try_streamed_forward(
        app,
        "review the file",
        "sid",
        lambda text: None,
        agent_override=module,
    )

    assert result is None
    fallback = _pop_stream_fallback(app, "sid")
    assert fallback["reason"] == "provider_streaming_unsupported"


def test_dynamic_agent_lm_config_preserves_claude_code_transport() -> None:
    from clio_agent.config import LMProviderConfig

    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(
            provider="claude_code",
            api_base="claude-code://exec",
            model="haiku",
            api_key="x",
            claude_code_transport="exec",
        )
    )

    # Step 6: the delegate returns a ResolvedLMSpec; materialize to the config.
    cfg = _dynamic_agent_lm_config(
        base_agent,
        AgentDef(
            id="earthscope",
            source="expert_pack",
            title="EarthScope",
            system_prompt="Use the EarthScope blueprint.",
        ),
    ).materialize()

    assert cfg.provider == "claude_code"
    assert cfg.claude_code_transport == "exec"


def test_build_stream_listeners_binds_known_predictors_explicitly() -> None:
    agent = _ExpertStreamingAgent()

    listeners = _build_stream_listeners(agent, _FakeStreamListener)

    assert [listener.signature_field_name for listener in listeners] == [
        "answer",
        "answer",
    ]
    assert all(listener.predict is not None for listener in listeners)
    assert listeners[0].predict is agent.chat_agent
    assert listeners[1].predict is agent.answer_synthesizer


async def test_expert_stream_responses_emit_live_field_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dspy.streaming.messages import StreamResponse

    audit_path = tmp_path / "stream-audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_path))
    captured: dict[str, Any] = {}

    def fake_streamify(program: Any, **kwargs: Any) -> Any:
        captured["program"] = program
        captured.update(kwargs)

        async def fake_streamed(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            yield StreamResponse(
                predict_name="data_expert.agent",
                signature_field_name="analysis",
                chunk="Analysis",
                is_last_chunk=False,
            )
            yield StreamResponse(
                predict_name="data_expert.agent",
                signature_field_name="recommendations",
                chunk="Do this",
                is_last_chunk=False,
            )
            yield dspy.Prediction(
                answer="Analysis\n\nRecommendations:\nDo this",
                selected_expert="data_expert",
                routing_rationale="",
            )

        return fake_streamed

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _ExpertStreamingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    result = await _try_streamed_forward(
        app,
        "stream expert",
        "sid",
        emit_chunk,
        session_mode="experts",
    )

    assert result is not None
    assert result.answer == "Analysis\n\nRecommendations:\nDo this"
    assert chunks == ["Analysis", "\n\nRecommendations:\n", "Do this"]
    assert captured["program"] is agent
    assert captured["is_async_program"] is False
    listeners = captured["stream_listeners"]
    assert all(listener.predict is not None for listener in listeners)
    assert {listener.signature_field_name for listener in listeners} == {"answer"}
    audit_rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    raw_events = [row for row in audit_rows if row["stage"] == "provider.raw_event"]
    assert [row["provider"] for row in raw_events] == [
        "dspy_streamify",
        "dspy_streamify",
        "dspy_streamify",
    ]
    assert {row["session_id"] for row in raw_events} == {"sid"}
    assert [row["source_channel"] for row in raw_events] == [
        "contract_delta",
        "contract_delta",
        "final_prediction",
    ]
    assert [row["signature_field_name"] for row in raw_events[:2]] == [
        "analysis",
        "recommendations",
    ]


async def test_stream_failure_after_delta_raises_instead_of_sync_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail_after_chunk(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        yield "partial "
        raise RuntimeError("stream transport lost")

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return fail_after_chunk

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _DspyAgent("sync fallback should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    with pytest.raises(_StreamingOutputError, match="stream transport lost"):
        await _try_streamed_forward(app, "stream breaks", "sid", emit_chunk)

    assert chunks == ["partial "]
    assert agent.calls == []


async def test_stream_failure_before_delta_raises_instead_of_sync_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail_before_chunk(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("planner/provider failed before output")
        yield "unreachable"

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return fail_before_chunk

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _DspyAgent("sync fallback should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    with pytest.raises(_StreamingOutputError, match="planner/provider failed"):
        await _try_streamed_forward(app, "stream breaks before output", "sid", emit_chunk)

    assert chunks == []
    assert agent.calls == []


async def test_stream_without_final_prediction_after_delta_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def stream_without_prediction(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        yield "partial "

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return stream_without_prediction

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _DspyAgent("sync fallback should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    chunks: list[str] = []

    async def emit_chunk(text: str) -> None:
        chunks.append(text)

    with pytest.raises(_StreamingOutputError, match="without a final prediction"):
        await _try_streamed_forward(app, "stream ends oddly", "sid", emit_chunk)

    assert chunks == ["partial "]
    assert agent.calls == []


def test_mid_stream_failure_surfaces_error_without_sync_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail_after_chunk(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        yield "partial "
        raise RuntimeError("stream transport lost")

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return fail_after_chunk

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _DspyAgent("sync fallback should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "provider_error"
    assert "stream transport lost" in assistant["error_info"]["message"]
    assert assistant["parts"][0]["text"] == "partial "

    history = app.state.bus._history.get(sid, [])
    completed_parts = [
        e
        for e in history
        if e.type == "message.part.completed" and e.payload.get("stream_source") == "live"
    ]
    completed_messages = [e for e in history if e.type == "message.completed"]
    assert completed_parts[-1].payload["final_text"] == "partial "
    assert completed_messages[-1].payload["stop_reason"] == "error"
    assert completed_messages[-1].payload["error_info"]["error"] == "provider_error"


def test_pre_stream_failure_surfaces_error_without_sync_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail_before_chunk(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("planner/provider failed before output")
        yield "unreachable"

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return fail_before_chunk

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    agent = _DspyAgent("sync fallback should not run")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "provider_error"
    assert "planner/provider failed" in assistant["error_info"]["message"]
    assert assistant["parts"] == []

    history = app.state.bus._history.get(sid, [])
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed_messages = [e for e in history if e.type == "message.completed"]
    assert deltas == []
    assert completed_messages[-1].payload["stop_reason"] == "error"
    assert completed_messages[-1].payload["error_info"]["details"]["partial_output"] is False
    assert completed_messages[-1].payload["error_info"]["details"]["stream_source"] == "batch"
    assert completed_messages[-1].payload["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(
        completed_messages[-1].payload["metadata"], "stream_failed_before_output"
    )
    assert (
        "RuntimeError" in completed_messages[-1].payload["metadata"]["stream_fallback"]["message"]
    )


def test_non_text_parts_skip_deltas(app_client) -> None:
    app, client, _ = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    )
    history = app.state.bus._history.get(sid, [])
    # routing_decision arrives via .added, not .delta.
    routing_added = [
        e
        for e in history
        if e.type == "message.part.added" and e.payload["part"]["type"] == "routing_decision"
    ]
    assert len(routing_added) == 1
    assert routing_added[0].payload["part"]["selected_agent"] == "data_expert"


def test_live_streamed_deltas_are_marked_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        await emit_chunk("Hel")
        await emit_chunk("lo")
        return _Pred(answer="Hello", selected_expert="", routing_rationale="")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(tmp_path / "semantic_traces"))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    added = [e for e in history if e.type == "message.part.added"]
    deltas = [e for e in history if e.type == "message.part.delta"]
    trace_backend = app.state.semantic_trace_backend
    trace_backend.flush()
    trace_rows = [
        line
        for line in (tmp_path / "semantic_traces" / f"{sid}.semantic.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    completed = [
        e
        for e in history
        if e.type == "message.part.completed" and e.payload.get("final_text") == "Hello"
    ]
    message_completed = [e for e in history if e.type == "message.completed"]

    assert [d.payload["delta"]["text_append"] for d in deltas] == ["Hel", "lo"]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert all(d.payload["signature_field_name"] == "answer" for d in deltas)
    text_added = [e for e in added if e.payload["part"]["type"] == "text"]
    assert text_added[-1].payload["part"]["metadata"]["signature_field_name"] == "answer"
    assert any(
        '"event_type": "lm.token.delta"' in row and '"delta": "Hel"' in row for row in trace_rows
    )
    assert any(
        '"event_type": "lm.token.delta"' in row and '"delta": "lo"' in row for row in trace_rows
    )
    assert all(e.type != "semantic.event" for e in history)
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "live"
    assert message_completed[-1].payload["metadata"]["stream_source"] == "live"
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
    assert text_parts[-1]["metadata"]["stream_source"] == "live"
    assert "stream_fallback" not in text_parts[-1]["metadata"]


def test_live_streamed_contract_fields_emit_normalized_transcript_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        await emit_chunk("thinking ", "main", "reasoning")
        await emit_chunk("next ", "main", "next_thought")
        await emit_chunk("answer", "main", "answer")
        return _Pred(answer="answer", selected_expert="", routing_rationale="")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    transcript_events = [e for e in history if e.type.startswith("turn.")]
    text_deltas = [e for e in history if e.type == "turn.text.delta"]

    assert transcript_events[0].type == "turn.started"
    assert [e.payload["field"] for e in text_deltas] == ["thought", "thought", "answer"]
    assert [e.payload["text_append"] for e in text_deltas] == [
        "thinking ",
        "next ",
        "answer",
    ]
    assert transcript_events[-1].type == "turn.completed"
    assert all("[[ ##" not in e.payload.get("text_append", "") for e in text_deltas)


def test_provider_aux_streams_as_model_aux_trace_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        await emit_chunk("raw provider thought", "main", "provider_thinking:claude_code_sdk")
        return _Pred(answer="done", selected_expert="", routing_rationale="")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    trace_deltas = [e for e in history if e.type == "turn.trace.delta"]

    assert len(trace_deltas) == 1
    assert trace_deltas[0].payload["trace_kind"] == "model_aux"
    assert trace_deltas[0].payload["text_append"] == "raw provider thought"


def _turn_id_of(history: list[Any]) -> str:
    user_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "user"
    ]
    assert len(user_created) == 1
    turn_id = user_created[0].payload["id"]
    # The user message correlates to its own turn (#711).
    assert user_created[0].payload["turn_id"] == turn_id
    return turn_id


def test_message_events_carry_turn_id_and_stream_source_batch(app_client) -> None:
    """#711: every assistant message.created / message.part.* / message.completed event
    carries the SAME turn_id (== the user message id) plus a stream_source, so a consumer
    joins assistant prose to the execution trajectory without heuristics."""

    app, client, _ = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "correlate me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    turn_id = _turn_id_of(history)

    part_events = [
        e
        for e in history
        if e.type in {"message.part.added", "message.part.delta", "message.part.completed"}
    ]
    assert part_events
    for e in part_events:
        assert e.payload["turn_id"] == turn_id, e.type
        assert e.payload["stream_source"] in {"live", "batch"}, e.type

    asst_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "assistant"
    ]
    assert asst_created
    assert all(e.payload["turn_id"] == turn_id for e in asst_created)

    completed = [e for e in history if e.type == "message.completed"]
    assert completed
    assert all(e.payload["turn_id"] == turn_id for e in completed)


def test_live_streamed_events_carry_turn_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#711 on the live path: lazily-created streamed assistant message + its deltas/
    completed events all correlate to the originating user turn."""

    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        await emit_chunk("Hel")
        await emit_chunk("lo")
        return _Pred(answer="Hello", selected_expert="", routing_rationale="")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    turn_id = _turn_id_of(history)

    deltas = [e for e in history if e.type == "message.part.delta"]
    assert deltas
    for e in deltas:
        assert e.payload["turn_id"] == turn_id
        assert e.payload["stream_source"] == "live"

    asst_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "assistant"
    ]
    assert asst_created
    assert all(e.payload["turn_id"] == turn_id for e in asst_created)


def test_streamify_final_prediction_without_chunks_has_specific_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def prediction_only_stream(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        yield dspy.Prediction(answer="complete answer", selected_expert="", routing_rationale="")

    def fake_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return prediction_only_stream

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fake_streamify)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_DspyAgent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    deltas = [e for e in history if e.type == "message.part.delta"]
    added = [
        e for e in history if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
    ]
    completed_parts = [
        e for e in history if e.type == "message.part.completed" and e.payload["stream_source"]
    ]
    completed_messages = [e for e in history if e.type == "message.completed"]

    assert deltas == []
    assert added[-1].payload["part"]["text"] == "complete answer"
    assert added[-1].payload["part"]["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(
        added[-1].payload["part"]["metadata"], "stream_completed_without_chunks"
    )
    assert completed_parts[-1].payload["stream_source"] == "batch"
    assert completed_parts[-1].payload["final_text"] == "complete answer"
    assert completed_messages[-1].payload["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(
        completed_messages[-1].payload["metadata"], "stream_completed_without_chunks"
    )


def test_streamed_answer_is_cleaned_once_whole_not_per_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B2: the CLIO contract-prose cleaner runs ONCE on the whole buffered answer
    at part close — never per streamed chunk. Per-chunk cleaning let the cleaner's
    multiline block/line regexes match a CHUNK boundary and leak a truncated
    fragment (the " station in a…" / "The acquis…" / "d MTA1…" artifacts). A
    sub-agent's streamed answer part (closed on the agent-change boundary) must
    equal the WHOLE-text clean, not the corrupt per-chunk concatenation."""
    from clio_agent.gact.delegation import _clean_public_transcript_text

    # A sub-agent answer whose "typed workflow_state" sentence straddles a chunk
    # boundary: per-chunk cleaning half-removes it and leaks "persisted." into the
    # prose; whole-text cleaning removes the complete sentence and keeps the rest.
    sub_chunks = [
        "I identified MTA1 as the nearest ranked station. ",
        "The typed workflow_state was ",
        "persisted. Coverage exists in the region.",
    ]
    sub_full = "".join(sub_chunks)
    sub_whole = _clean_public_transcript_text(sub_full, preserve_whitespace=True)

    def _per_chunk(cs: list[str]) -> str:
        out: list[str] = []
        for c in cs:
            t = _clean_public_transcript_text(c, preserve_whitespace=True)
            if t:
                out.append(t)
        return "".join(out)

    per_chunk = _per_chunk(sub_chunks)
    # Precondition: the two strategies genuinely differ for this input, else the
    # test proves nothing. Per-chunk leaks the mid-sentence "persisted" fragment.
    assert per_chunk != sub_whole
    assert "persisted" in per_chunk
    assert "persisted" not in sub_whole

    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _Pred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        for chunk in sub_chunks:
            await emit_chunk(chunk, "data", "answer")
        # Agent change → closes the "data" answer part (whole-text clean applies).
        await emit_chunk("Final orchestrator answer.", "main", "answer")
        return _Pred(answer="Final orchestrator answer.", selected_expert="", routing_rationale="")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "go"}]},
    )

    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    data_parts = [
        p
        for p in assistant["parts"]
        if p["type"] == "text" and p.get("agent_id") == "data" and (p["text"] or "").strip()
    ]
    assert data_parts, "expected the sub-agent (data) streamed answer part"
    body = data_parts[-1]["text"]
    # The persisted sub-agent answer is the WHOLE-cleaned text — no leaked
    # "persisted." fragment, surrounding prose intact (no mid-sentence truncation).
    assert body == sub_whole
    assert body != per_chunk
    assert "I identified MTA1 as the nearest ranked station." in body
    assert "Coverage exists in the region." in body
    assert "persisted" not in body


def test_streamed_field_buffer_cleared_at_turn_end_and_turn_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """iowarp/clio-agent#757: ``app.state.live_streamed_field_text`` must be
    cleared at turn end, and the finalize thinking-part suppression must match
    only against the CURRENT turn's streamed text.

    Turn 1 streams contract reasoning live; turn 2 streams nothing but its
    finalize ``reasoning`` repeats turn 1's phrasing. Before the fix the buffer
    survived turn 1, so turn 2's thinking part was wrongly suppressed as
    "already streamed" and the dict grew forever.
    """
    from .conftest import complete_turn

    repeated = "I will inspect the HDF5 schema before answering the user."

    @dataclass
    class _ReasoningPred:
        answer: str = ""
        selected_expert: str = ""
        routing_rationale: str = ""
        reasoning: str = ""

    calls = {"n": 0}

    async def fake_streamed_forward(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
    ) -> _ReasoningPred:
        del app, enriched_text, sid, session_mode, session_edit_mode
        calls["n"] += 1
        if calls["n"] == 1:
            # Turn 1: the reasoning channel streams live -> recorded in the buffer.
            await emit_chunk(repeated, "main", "reasoning")
            await emit_chunk("turn one answer", "main", "answer")
            return _ReasoningPred(answer="turn one answer")
        # Turn 2: nothing streams; the finalize reasoning repeats turn 1's phrasing.
        return _ReasoningPred(answer="turn two answer", reasoning=repeated)

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    complete_turn(client, sid, "turn one")
    store = getattr(app.state, "live_streamed_field_text", {}) or {}
    assert store.get(sid) in (None, {}), (
        f"live_streamed_field_text must be cleared at turn end, got: {store.get(sid)!r}"
    )

    assistant2 = complete_turn(client, sid, "turn two")
    thinking_texts = [p["text"] for p in assistant2["parts"] if p["type"] == "thinking"]
    assert thinking_texts == [repeated], (
        "turn 2's thinking part must NOT be suppressed by turn 1's streamed text"
    )
    store = getattr(app.state, "live_streamed_field_text", {}) or {}
    assert store.get(sid) in (None, {})


def test_live_streamed_field_buffer_helpers_are_turn_scoped() -> None:
    """Unit coverage for the #757 turn-scoped buffer helpers: a stale entry from
    a previous turn is never matched and is dropped (with a structured warning)
    on the next write; clearing removes the session's entry entirely."""
    from clio_agent.gact.streaming import (
        _clear_live_streamed_field_text,
        _live_streamed_field_text_for_turn,
        _record_live_streamed_field_text,
    )

    app = SimpleNamespace(state=SimpleNamespace())
    sid = "sess_1"

    _record_live_streamed_field_text(app, sid, "turn_1", "main", "reasoning", "alpha ")
    _record_live_streamed_field_text(app, sid, "turn_1", "main", "reasoning", "beta")
    assert _live_streamed_field_text_for_turn(app, sid, "turn_1", "main", "reasoning") == (
        "alpha beta"
    )
    # A DIFFERENT turn must never see turn_1's text (per-turn suppression scope).
    assert _live_streamed_field_text_for_turn(app, sid, "turn_2", "main", "reasoning") == ""
    # Writing under a new turn drops the stale turn_1 residue instead of appending.
    _record_live_streamed_field_text(app, sid, "turn_2", "main", "reasoning", "gamma")
    assert _live_streamed_field_text_for_turn(app, sid, "turn_2", "main", "reasoning") == "gamma"
    assert _live_streamed_field_text_for_turn(app, sid, "turn_1", "main", "reasoning") == ""
    # End-of-turn cleanup empties the session's entry.
    _clear_live_streamed_field_text(app, sid)
    assert app.state.live_streamed_field_text == {}
