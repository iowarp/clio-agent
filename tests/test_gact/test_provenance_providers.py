"""Provider contracts and provider-neutral execution provenance."""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app
from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher
from clio_agent.gact.provenance.factory import configured_provider_names
from clio_agent.gact.provenance.flowcept import (
    FlowceptProvenanceProvider,
    FlowceptProviderConfig,
    _normalize_flowcept_records,
    _safe_value,
)
from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ProviderReceipt
from clio_agent.gact.semantic_events import SemanticEvent
from tests._config_layer import set_config


class _RecordingProvider:
    name = "recording"
    durable = False
    queryable = False

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[SemanticEvent] = []
        self.fail = fail
        self.closed = False

    def emit(self, event: SemanticEvent) -> ProviderReceipt:
        if self.fail:
            raise RuntimeError("downstream unavailable")
        self.events.append(event)
        return ProviderReceipt.ACCEPTED

    def close(self) -> None:
        self.closed = True


def _event(
    event_type: str = "turn.started",
    *,
    status: str = "started",
    occurred_at: str = "2026-08-21T12:00:00+00:00",
) -> SemanticEvent:
    return SemanticEvent(
        event_type=event_type,
        session_id="sess_root",
        workspace_id="ws_science",
        trace_id="trace_1",
        turn_id="turn_1",
        status=status,
        occurred_at=occurred_at,
        actor={"agent_id": "earthscope"},
        payload={"input": "sensitive prompt", "expert_span_id": "expert_1"},
    )


def test_jsonl_is_default_provider() -> None:
    assert configured_provider_names() == ["jsonl"]


def test_jsonl_selection_does_not_import_flowcept(monkeypatch: pytest.MonkeyPatch) -> None:
    set_config("provenance.agentic.providers", ["jsonl"])
    real_import = importlib.import_module

    def guarded(name: str, package: str | None = None):
        if name == "flowcept" or name.startswith("flowcept."):
            raise AssertionError("Flowcept imported while not selected")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded)
    assert configured_provider_names() == ["jsonl"]


def test_selected_missing_flowcept_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = importlib.import_module

    def missing(name: str, package: str | None = None):
        if name == "flowcept":
            raise ModuleNotFoundError("flowcept")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"clio-agent\[flowcept\]"):
        FlowceptProvenanceProvider(FlowceptProviderConfig())


def test_dispatcher_isolates_provider_failure() -> None:
    good = _RecordingProvider()
    bad = _RecordingProvider(fail=True)
    bad.name = "bad"
    dispatcher = ProvenanceDispatcher([good, bad], queue_size=4)
    dispatcher.emit(_event())
    dispatcher.close()

    assert [event.event_type for event in good.events] == ["turn.started"]
    health = {row["name"]: row for row in dispatcher.health()}
    assert health["bad"]["failed"] == 1
    assert health["bad"]["status"] == "degraded"
    assert good.closed and bad.closed


def test_flowcept_privacy_removes_content_fields() -> None:
    value = {
        "prompt": "private",
        "api_key": "must-never-export",
        "nested": {"reasoning": "chain", "model": "gpt-5.6-luna"},
        "artifact_id": "artifact_1",
    }
    assert _safe_value(value, redact=True) == {
        "prompt": "[redacted]",
        "api_key": "[redacted]",
        "nested": {"reasoning": "[redacted]", "model": "gpt-5.6-luna"},
        "artifact_id": "artifact_1",
    }
    assert _safe_value(value, redact=False)["api_key"] == "[redacted]"


def test_flowcept_query_uses_same_span_pairing_as_native() -> None:
    def task(event_id: str, event_type: str, status: str, occurred_at: float) -> dict[str, Any]:
        return {
            "task_id": event_id,
            "workflow_id": "workflow_1",
            "campaign_id": "campaign_1",
            "started_at": occurred_at,
            "ended_at": occurred_at,
            "status": "FINISHED",
            "custom_metadata": {
                "clio": {
                    "event_id": event_id,
                    "event_type": event_type,
                    "event_status": status,
                    "session_id": "sess_root",
                    "workspace_id": "ws_science",
                    "trace_id": "trace_1",
                    "turn_id": "turn_1",
                    "span_id": event_id,
                    "correlation_id": "turn_1",
                    "summary": event_type,
                }
            },
        }

    result = _normalize_flowcept_records(
        session_id="sess_root",
        workflows=[{"workflow_id": "workflow_1", "campaign_id": "campaign_1"}],
        tasks=[
            task("started", "turn.started", "started", 1_775_000_000.0),
            task("completed", "turn.completed", "completed", 1_775_000_002.0),
        ],
        agents=[],
        provider_health={"mongo": "ok"},
        truncated=False,
    )

    assert result["provider"] == "flowcept"
    assert result["complete"] is True
    assert len(result["spans"]) == 1
    assert result["spans"][0]["duration_ms"] == 2000.0
    assert len(result["spans"][0]["attributes"]["flowcept_records"]) == 2


@pytest.mark.parametrize("scope", ["session", "workspace", "agent"])
def test_flowcept_campaign_scopes_are_deterministic(scope: str) -> None:
    provider = object.__new__(FlowceptProvenanceProvider)
    provider.config = FlowceptProviderConfig(campaign_scope=scope)
    provider._sessions = {"sess_root": {"workspace_id": "ws_science", "agent_id": "earthscope"}}
    assert provider._campaign_id(_event()) == provider._campaign_id(_event())


def test_normalization_pairs_started_and_completed_events() -> None:
    started = _event().to_dict()
    completed_event = _event(
        "turn.completed",
        status="completed",
        occurred_at="2026-08-21T12:00:02+00:00",
    )
    completed_event.span_id = "completed_event"
    completed = completed_event.to_dict()
    result = normalize_semantic_events(
        [started, completed], provider="native", session_id="sess_root"
    )
    assert len(result["spans"]) == 1
    assert result["spans"][0]["duration_ms"] == 2000.0
    assert result["complete"] is True


def test_normalization_pairs_cross_family_lifecycles_and_ignores_running_samples() -> None:
    rows = [
        _event("expert.lifecycle.started", status="running").to_dict(),
        _event("expert.extract.completed", status="completed").to_dict(),
        _event("llm.request.started", status="running").to_dict(),
        _event("llm.request.started", status="running").to_dict(),
        _event("llm.response.completed", status="completed").to_dict(),
        _event("lm.token.delta", status="running").to_dict(),
    ]

    result = normalize_semantic_events(rows, provider="native", session_id="sess_root")

    assert result["complete"] is True
    assert [span["event_type"] for span in result["spans"]] == [
        "expert.lifecycle.started",
        "llm.request.started",
        "lm.token.delta",
    ]
    assert len(result["spans"][1]["source_event_ids"]) == 3
    assert result["spans"][2]["end_time"] is None


def test_execution_endpoint_is_provider_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace_dir = tmp_path / "traces"
    set_config("provenance.agentic.providers", ["jsonl"])
    set_config("provenance.agentic.jsonl.path", str(trace_dir))
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=_MemoryStore())
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None, arc=arc)
    with TestClient(app) as client:
        session = client.post("/v1/sessions", json={"title": "provenance"}).json()
        sid = session["id"]
        monkeypatch.setattr(
            "clio_agent.gact.routes.provenance._native_events",
            lambda _app, _session_ids: [_event().to_dict()],
        )
        response = client.get(
            f"/v1/sessions/{sid}/provenance/execution",
            params={"provider": "native"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "native"
        assert body["schema_version"] == "clio.execution_provenance.v1"
        assert any(span["event_type"] == "session.created" for span in body["spans"])

        providers = client.get("/v1/provenance/providers").json()
        assert providers["default_provider"] == "native"
        assert providers["providers"][0]["name"] == "native"

        client.delete(f"/v1/sessions/{sid}")
    # The dispatcher closes after the lifespan and all accepted writes are drained.
    time.sleep(0.01)
    assert (trace_dir / f"{sid}.semantic.jsonl").is_file()
