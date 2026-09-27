"""HTTP surface: availability + selection over the real-run fixture."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.attention import routes as attention_routes
from tests.test_gact.test_attention._support import (
    SID,
    FixtureRenderer,
    fixture,
    fixture_flowcept,
    fixture_transcript,
)


class _Backend:
    def __init__(self, readers: dict[str, Any]) -> None:
        self._readers = readers

    def reader(self, name: str) -> Any:
        return self._readers.get(name)

    def flush(self) -> None:
        return None


def _journal(tmp_path: Path, model: str | None = None) -> Path:
    c = fixture()["lm_call"]
    event = {
        "event_type": "lm.call",
        "event_id": c["event_id"],
        "session_id": SID,
        "turn_id": c["turn_id"],
        "occurred_at": "2026-09-21T18:40:00Z",
        "payload": {
            "model": model or c["model"],
            "messages": c["messages"],
            "content": c["content"],
            "response_id": c["response_id"],
        },
    }
    path = tmp_path / f"{SID}.semantic.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return tmp_path


def _client(readers: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.state.messages = {SID: fixture_transcript()}
    app.state.semantic_trace_backend = _Backend(readers)
    renderer = FixtureRenderer()
    monkeypatch.setattr(attention_routes, "_renderer", lambda identity: renderer)
    attention_routes.register_attention_routes(app)
    return TestClient(app)


def test_availability_and_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    client = _client(readers, monkeypatch)
    avail = client.get(f"/v1/sessions/{SID}/messages/msg_asst_1/attention/availability").json()
    assert avail["available"] is True and avail["top_pct"] == 10.0
    body = {"part_id": "call_sel", "field": "thought", "start": 0, "end": 59}
    result = client.post(f"/v1/sessions/{SID}/messages/msg_asst_1/attention", json=body).json()
    assert result["available"] is True
    assert result["selection"]["steps"] == [133, 146]
    assert result["sources"]


def test_user_message_availability_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    turn = fixture()["lm_call"]["turn_id"]
    got = _client(readers, monkeypatch).get(
        f"/v1/sessions/{SID}/messages/{turn}/attention/availability"
    )
    assert got.status_code == 200
    assert got.json()["reason"] == "message_not_generated"


def test_without_flowcept_the_reason_is_flowcept_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path))}
    got = (
        _client(readers, monkeypatch)
        .get(f"/v1/sessions/{SID}/messages/msg_asst_1/attention/availability")
        .json()
    )
    assert got == {
        "available": False,
        "reason": "flowcept_not_configured",
        "message": "Flowcept provenance is not configured.",
        "detail": "add 'flowcept' to provenance.agentic.providers",
        "context": {},
    }


def test_non_vllm_session_is_provider_not_vllm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {
        "jsonl": SimpleNamespace(path=_journal(tmp_path, model="claude_code/sonnet")),
        "flowcept": fixture_flowcept(),
    }
    got = (
        _client(readers, monkeypatch)
        .get(f"/v1/sessions/{SID}/messages/msg_asst_1/attention/availability")
        .json()
    )
    assert got["reason"] == "provider_not_vllm"


def test_lm_calls_fall_back_to_flowcept_records(monkeypatch: pytest.MonkeyPatch) -> None:
    c = fixture()["lm_call"]
    row = {
        "subtype": "ai_model_invocation",
        "task_id": "t1",
        "started_at": "2026-09-21T18:40:00Z",
        "custom_metadata": {
            "clio": {
                "event_type": "lm.call",
                "event_id": c["event_id"],
                "session_id": SID,
                "turn_id": c["turn_id"],
                "response_id": c["response_id"],
                "payload": {
                    "model": c["model"],
                    "messages": c["messages"],
                    "content": c["content"],
                },
            }
        },
    }
    flowcept = fixture_flowcept()
    flowcept.tasks.append(row)
    client = _client({"flowcept": flowcept}, monkeypatch)
    body = {"part_id": "call_sel", "field": "thought", "start": 0, "end": 59}
    result = client.post(f"/v1/sessions/{SID}/messages/msg_asst_1/attention", json=body).json()
    assert result["available"] is True
