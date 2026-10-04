"""HTTP surface: session availability + selection over the real-run fixture."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.attention import routes as attention_routes
from clio_agent.gact.attention.transcript_map import transcript_texts
from clio_agent.gact.parts import Part
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


def _availability(client: TestClient) -> dict[str, Any]:
    return client.get(f"/v1/sessions/{SID}/attention/availability").json()


def test_content_references_page_exact_parts_and_reject_another_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client({}, monkeypatch)
    message = client.app.state.messages[SID][-1]
    message.parts = [Part(id=f"p{i}", type="text", text=f"Text {i}") for i in range(103)]
    path = f"/v1/sessions/{SID}/messages/{message.id}/attention/content"
    page = client.get(path)
    assert page.status_code == 200
    assert len(page.json()["items"]) == 100
    assert page.json()["next_cursor"] == 100
    rest = client.get(path, params={"cursor": 100}).json()
    assert [item["reference"]["part_id"] for item in rest["items"]] == ["p100", "p101", "p102"]
    assert rest["next_cursor"] is None
    assert client.get(path.replace(SID, "another-session")).status_code == 404
    assert client.get(path, params={"cursor": -1}).status_code == 422


def test_profile_parameters_are_validated_before_capture_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client({}, monkeypatch)
    for profile in ({"decay_base": 0}, {"direction": "guessed"}, {"version": 2}):
        result = client.post(
            f"/v1/sessions/{SID}/messages/msg_asst_1/attention", json={"profile": profile}
        )
        assert result.status_code == 422


def test_availability_marks_the_recorded_vllm_answer_and_selection_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    client = _client(readers, monkeypatch)
    avail = _availability(client)
    # the earlier-turn answer has no recorded vLLM call, the fixture call's answer does
    assert avail == {
        "enabled": True,
        "messages": {"msg_asst_earlier": False, "msg_asst_1": True},
    }
    body = {"part_id": "call_sel", "field": "thought", "start": 0, "end": 59}
    result = client.post(f"/v1/sessions/{SID}/messages/msg_asst_1/attention", json=body).json()
    assert result["available"] is True
    assert result["selection"]["steps"] == [133, 146]
    assert result["sources"]


def test_attention_off_disables_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "0")
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    avail = _availability(_client(readers, monkeypatch))
    assert avail["enabled"] is False and avail["reason"] == "attention_disabled"
    assert avail["messages"] == {}


def test_without_flowcept_the_session_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path))}
    avail = _availability(_client(readers, monkeypatch))
    assert avail["enabled"] is False and avail["reason"] == "flowcept_not_configured"


def test_non_vllm_answers_are_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {
        "jsonl": SimpleNamespace(path=_journal(tmp_path, model="claude_code/sonnet")),
        "flowcept": fixture_flowcept(),
    }
    avail = _availability(_client(readers, monkeypatch))
    assert avail["enabled"] is True
    assert avail["messages"]["msg_asst_1"] is False


def test_availability_reads_no_attention_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the Mongo record is needed: an unreachable file still reads as available."""
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_FILES_DIR", str(tmp_path / "nowhere"))
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    assert _availability(_client(readers, monkeypatch))["messages"]["msg_asst_1"] is True


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


def test_lookup_validates_references_and_reads_recorded_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    client = _client(readers, monkeypatch)
    source = next(t for t in transcript_texts(fixture_transcript()) if t.part_id == "u0")
    reference = {
        "session_id": SID,
        "message_id": source.message_id,
        "part_id": source.part_id,
        "field": source.field,
        "content_revision": source.content_revision,
        "selection": {"kind": "whole"},
    }
    body = {"direction": "source_to_generation", "selections": [reference]}
    response = client.post(f"/v1/sessions/{SID}/attention/lookup", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["schema"] == "clio.attention.lookup.v1"
    assert len(result["views"]) == 1
    assert result["views"][0]["sources"][0]["content_revision"] == source.content_revision
    assert result["next_cursor"] is None
    for invalid in ({"selections": []}, {"limit": 33}, {"profile": {"decay_base": 0}}):
        assert (
            client.post(
                f"/v1/sessions/{SID}/attention/lookup", json={**body, **invalid}
            ).status_code
            == 422
        )
