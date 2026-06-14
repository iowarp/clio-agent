"""Tests for the semantic-event projection registry and config defaults (S6).

The capture-vs-projection split (S1) records one full event and reduces it per
consumer. These guard the handoff projection modes and the deferred history
stub, plus the finalized default that the durable file backend is now on.
"""

import pytest

from clio_agent.gact import semantic_events as se
from clio_agent.gact.semantic_events import (
    SemanticEvent,
    build_trace_backend,
    project_full,
    project_handoff,
    project_history,
    project_sse,
)


def _handoff_event():
    return SemanticEvent(
        event_type="expert.response.completed",
        session_id="s1",
        trace_id="trace_t1",
        turn_id="t1",
        actor={"agent_id": "data"},
        payload={
            "answer": "71 stations near San Diego.",
            "result_summary": "found stations",
            "tools_called": [{"name": "shell_bash", "ok": True}],
            "workflow_state": {"stage": "complete"},
            "evidence": [{"source": "iris"}],
            "reasoning": "long chain of thought" * 100,
            "trajectory": {"tool_name_0": "shell_bash"},
        },
    )


def test_project_handoff_final_keeps_evidence_strips_reasoning():
    out = project_handoff(_handoff_event(), "FINAL")
    payload = out["payload"]
    assert payload["answer"].startswith("71 stations")
    assert "tools_called" in payload
    assert "workflow_state" in payload
    assert "evidence" in payload
    # Heavy fields dropped by the FINAL handoff reduction.
    assert "reasoning" not in payload
    assert "trajectory" not in payload


def test_project_handoff_summary_keeps_only_answer():
    out = project_handoff(_handoff_event(), "SUMMARY")
    payload = out["payload"]
    assert set(payload.keys()) <= {"answer", "result_summary"}
    assert payload["answer"].startswith("71 stations")
    assert "tools_called" not in payload


def test_project_full_keeps_everything_unredacted():
    out = project_full(_handoff_event())
    payload = out["payload"]
    # Full capture: reasoning + trajectory present and NOT redacted.
    assert payload["trajectory"] == {"tool_name_0": "shell_bash"}
    assert payload["reasoning"].startswith("long chain")


def test_project_sse_redacts_sensitive_but_keeps_envelope():
    out = project_sse(_handoff_event())
    # Envelope (ids/type) always present and unredacted.
    assert out["event_type"] == "expert.response.completed"
    assert out["trace_id"] == "trace_t1"
    # Sensitive bodies redacted in the SSE projection.
    assert str(out["payload"]["reasoning"]).startswith("[redacted]")
    assert str(out["payload"]["trajectory"]).startswith("[redacted]")


def test_project_history_is_deferred():
    with pytest.raises(NotImplementedError):
        project_history(_handoff_event())


def test_default_trace_backend_is_none_optin(tmp_path, monkeypatch):
    # Default is opt-in (none) pending the turn-task-robustness fix that lets the
    # durable file backend be default-on; the off-loop file backend is enabled
    # explicitly (grind/research) via CLIO_SEMANTIC_TRACE_BACKEND=file.
    monkeypatch.delenv("CLIO_SEMANTIC_TRACE_BACKEND", raising=False)
    monkeypatch.delenv("CLIO_SEMANTIC_TRACE_PATH", raising=False)
    backend = build_trace_backend(tmp_path / "semantic_traces")
    assert backend.name == "none"
    assert isinstance(backend, se.NoopSemanticTraceBackend)


def test_trace_backend_file_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(tmp_path / "traces"))
    backend = build_trace_backend(tmp_path / "semantic_traces")
    assert backend.name == "file"
    backend.close()
