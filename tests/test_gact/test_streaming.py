"""CLIO-BBBBBBBBBB19: text parts stream via message.part.delta events."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _StreamingOutputError,
    _try_streamed_forward,
    build_app,
)


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


@pytest.fixture()
def app_client(tmp_path: Path):
    answer = "X" * 200  # 200 chars -> 4 chunks at 64-char window.
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(answer))
    return app, TestClient(app), answer


def test_text_parts_stream_as_deltas(app_client) -> None:
    app, client, answer = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    added = [
        e for e in history
        if e.type == "message.part.added"
        and e.payload["part"]["type"] == "text"
    ]
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed = [e for e in history if e.type == "message.part.completed"]

    # The text part arrives as one .added (empty text) + N .deltas +
    # one .completed. The other part types (routing_decision) use
    # .added with full content and emit no deltas.
    assert len(added) == 1
    assert added[0].payload["part"]["text"] == ""
    assert added[0].payload["part"]["metadata"]["stream_source"] == "synthetic_posthoc"
    assert len(deltas) == 4
    assert all(d.payload["stream_source"] == "synthetic_posthoc" for d in deltas)
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "synthetic_posthoc"

    # Concatenated deltas reconstruct the full answer.
    chunks = [d.payload["delta"]["text_append"] for d in deltas]
    assert "".join(chunks) == answer


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
        e for e in history
        if e.type == "message.part.completed"
        and e.payload.get("stream_source") == "live"
    ]
    completed_messages = [e for e in history if e.type == "message.completed"]
    assert completed_parts[-1].payload["final_text"] == "partial "
    assert completed_messages[-1].payload["stop_reason"] == "error"
    assert completed_messages[-1].payload["error_info"]["error"] == "provider_error"


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
        e for e in history
        if e.type == "message.part.added"
        and e.payload["part"]["type"] == "routing_decision"
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

    monkeypatch.setattr(
        "clio_agent.gact.app._try_streamed_forward", fake_streamed_forward
    )
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
        e for e in history
        if e.type == "message.part.completed"
        and e.payload.get("final_text") == "Hello"
    ]
    message_completed = [e for e in history if e.type == "message.completed"]

    assert [d.payload["delta"]["text_append"] for d in deltas] == ["Hel", "lo"]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "live"
    assert message_completed[-1].payload["metadata"]["stream_source"] == "live"
