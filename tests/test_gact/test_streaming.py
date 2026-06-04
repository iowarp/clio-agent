"""CLIO-BBBBBBBBBB19: text parts stream via message.part.delta events."""

from __future__ import annotations

import importlib
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


async def test_provider_without_live_streaming_skips_streamify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("streamify should not be called for non-streaming providers")

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    agent = _DspyAgent("sync answer")
    agent._provider_config = SimpleNamespace(provider="claude_code")
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


@pytest.mark.asyncio
async def test_argonne_provider_skips_streamify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("streamify should not be called for Argonne providers")

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    agent = _DspyAgent("sync answer")
    agent._provider_config = SimpleNamespace(provider="argonne")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    result = await _try_streamed_forward(app, "hello", "sid", lambda text: None)

    assert result is None
    assert agent.calls == []
    fallback = _pop_stream_fallback(app, "sid")
    assert fallback["reason"] == "provider_streaming_unsupported"


@pytest.mark.asyncio
async def test_argonne_preset_id_skips_streamify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_streamify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("streamify should not be called for Argonne preset ids")

    streamify_module = importlib.import_module("dspy.streaming.streamify")
    monkeypatch.setattr(streamify_module, "streamify", fail_streamify)
    agent = _DspyAgent("sync answer")
    agent._provider_config = SimpleNamespace(provider="argonne_sophia")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    result = await _try_streamed_forward(app, "hello", "sid", lambda text: None)

    assert result is None
    assert agent.calls == []
    fallback = _pop_stream_fallback(app, "sid")
    assert fallback["reason"] == "provider_streaming_unsupported"


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
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed = [
        e
        for e in history
        if e.type == "message.part.completed" and e.payload.get("final_text") == "Hello"
    ]
    message_completed = [e for e in history if e.type == "message.completed"]

    assert [d.payload["delta"]["text_append"] for d in deltas] == ["Hel", "lo"]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "live"
    assert message_completed[-1].payload["metadata"]["stream_source"] == "live"
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
    assert text_parts[-1]["metadata"]["stream_source"] == "live"
    assert "stream_fallback" not in text_parts[-1]["metadata"]


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
