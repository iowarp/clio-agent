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
from clio_agent.gact.provenance.factory import _LegacyFactoryProvider, configured_provider_names
from clio_agent.gact.provenance.flowcept import (
    FlowceptProvenanceProvider,
    FlowceptProviderConfig,
    _normalize_flowcept_records,
    _safe_value,
)
from clio_agent.gact.provenance.jsonl import JsonlProvenanceProvider
from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ProvenanceProvider, ProviderReceipt
from clio_agent.gact.semantic_events import SemanticEvent
from tests._config_layer import set_config


class _RecordingProvider:
    name = "recording"
    durable = False
    queryable = False

    def __init__(
        self,
        *,
        fail: bool = False,
        flush_fail: bool = False,
        emit_delay: float = 0.0,
        calls: list[str] | None = None,
    ) -> None:
        self.events: list[SemanticEvent] = []
        self.fail = fail
        self.flush_fail = flush_fail
        self.emit_delay = emit_delay
        self.closed = False
        self.flushed = False
        # Shared, order-sensitive call log -- distinct from ``events``
        # (which only records ACCEPTED emits) so a slow/failing emit still
        # shows up in the ordering a caller cares about (D3a/D3b).
        self.calls: list[str] = calls if calls is not None else []

    def emit(self, event: SemanticEvent) -> ProviderReceipt:
        if self.emit_delay:
            time.sleep(self.emit_delay)
        if self.fail:
            self.calls.append("emit-failed")
            raise RuntimeError("downstream unavailable")
        self.events.append(event)
        self.calls.append("emit")
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        self.flushed = True
        if self.flush_fail:
            self.calls.append("flush-failed")
            raise RuntimeError("flush unavailable")
        self.calls.append("flush")

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


def test_dispatcher_flush_waits_for_a_slow_emit_before_calling_provider_flush() -> None:
    """D3(a): flush() must not call provider.flush() until the worker's OWN
    queue has actually drained -- a slow emit proves the ordering, not just
    the eventual outcome (the #1247 CI regression was exactly a caller
    observing flush() as complete before the provider had finished its own
    work)."""

    calls: list[str] = []
    provider = _RecordingProvider(emit_delay=0.2, calls=calls)
    dispatcher = ProvenanceDispatcher([provider], queue_size=4)

    dispatcher.emit(_event())
    dispatcher.flush()
    dispatcher.close()

    assert calls == ["emit", "flush"]
    assert provider.flushed is True


def test_dispatcher_flush_contains_a_raising_provider_and_still_flushes_the_next() -> None:
    """D3(b): a provider whose flush() raises must not escape ProvenanceDispatcher.flush()
    (it would otherwise become an unhandled 500 from
    GET /v1/sessions/{sid}/provenance/execution, which calls flush() inline)
    and must not skip the NEXT provider's join+flush."""

    bad_calls: list[str] = []
    bad = _RecordingProvider(flush_fail=True, calls=bad_calls)
    bad.name = "bad"
    good_calls: list[str] = []
    good = _RecordingProvider(calls=good_calls)
    good.name = "good"
    dispatcher = ProvenanceDispatcher([bad, good], queue_size=4)

    dispatcher.emit(_event())
    dispatcher.flush()  # must not raise
    dispatcher.close()

    assert bad.flushed is True
    assert "flush-failed" in bad_calls
    assert good.flushed is True
    assert "flush" in good_calls

    health = {row["name"]: row for row in dispatcher.health()}
    assert health["bad"]["failed"] == 1
    assert health["bad"]["status"] == "degraded"
    assert "flush unavailable" in health["bad"]["last_error"]
    # The good provider's own health is untouched by its sibling's failure.
    assert health["good"]["failed"] == 0
    assert health["good"]["status"] == "ready"


def test_dispatcher_flush_first_failure_is_loud(caplog: pytest.LogCaptureFixture) -> None:
    """Mirrors the existing emit-side loud-first-failure contract
    (test_dispatcher_first_provider_failure_is_loud in
    test_artifact_provenance_providers.py) for the flush side added here."""

    provider = _RecordingProvider(flush_fail=True)
    dispatcher = ProvenanceDispatcher([provider], queue_size=4)
    with caplog.at_level("WARNING", logger="clio_agent.gact.provenance.dispatcher"):
        dispatcher.emit(_event())
        dispatcher.flush()
        dispatcher.emit(_event())
        dispatcher.flush()
        dispatcher.close()

    warnings = [r for r in caplog.records if "degraded on flush" in r.getMessage()]
    assert len(warnings) == 1, "first flush failure loud, repeats counted in health only"
    health = dispatcher.health()
    assert health[0]["failed"] == 2


def test_synchronous_providers_satisfy_the_provenance_protocol(tmp_path: Path) -> None:
    """D3(c): flush() is REQUIRED (protocol.py), not duck-typed -- exercise
    it on a provider with a genuine async writer behind it (jsonl) and one
    whose emit() is already synchronous (the recording double), proving
    both conform and neither's flush() raises."""

    jsonl_provider = JsonlProvenanceProvider(tmp_path / "trace")
    assert isinstance(jsonl_provider, ProvenanceProvider)
    jsonl_provider.flush()  # nothing enqueued; must not raise

    recording = _RecordingProvider()
    assert isinstance(recording, ProvenanceProvider)
    recording.flush()
    assert recording.flushed is True


def test_legacy_factory_provider_flush_proxies_when_backend_has_one() -> None:
    """D2: a factory backend that DOES expose flush() gets a real barrier,
    declared explicitly (flush_durable=True, no note)."""

    class _FlushableBackend:
        name = "custom"

        def __init__(self) -> None:
            self.flushed = False

        def emit(self, event: Any) -> None:
            return None

        def flush(self) -> None:
            self.flushed = True

        def close(self) -> None:
            return None

    backend = _FlushableBackend()
    provider = _LegacyFactoryProvider(backend)

    assert provider.flush_durable is True
    assert provider.flush_note == ""
    provider.flush()
    assert backend.flushed is True


def test_legacy_factory_provider_flush_is_a_declared_gap_without_a_backend_flush() -> None:
    """D2: a factory backend with NO flush() gets an honest no-op that never
    raises, with the gap declared (never a bare pass) via flush_durable/
    flush_note so GET /v1/provenance/providers can surface it."""

    class _NoFlushBackend:
        name = "custom"

        def emit(self, event: Any) -> None:
            return None

        def close(self) -> None:
            return None

    provider = _LegacyFactoryProvider(_NoFlushBackend())

    assert provider.flush_durable is False
    assert provider.flush_note  # non-empty, structured
    provider.flush()  # must not raise


def test_flowcept_provider_declares_flush_as_a_non_durable_no_op() -> None:
    """D2: Flowcept has no repeatable drain API (verified against the
    installed flowcept 1.0.3 source: only a one-time terminal stop()), so
    it must declare flush_durable=False with a note rather than faking a
    barrier, and flush() itself must be a safe no-op."""

    assert FlowceptProvenanceProvider.flush_durable is False
    assert FlowceptProvenanceProvider.flush_note

    provider = object.__new__(FlowceptProvenanceProvider)  # bypass __init__
    provider.flush()  # must not raise even without a real Flowcept runtime


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
