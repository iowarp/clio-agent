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


def test_project_sse_keeps_content_redacts_only_secrets():
    out = project_sse(_handoff_event())
    # Envelope (ids/type) always present and unredacted.
    assert out["event_type"] == "expert.response.completed"
    assert out["trace_id"] == "trace_t1"
    # Content (reasoning/trajectory/answer) is the user's OWN session trajectory — it
    # is NOT redacted on the SSE stream. CLIO does not redact its own trajectory; the
    # full content flows to the live UI (only genuine secrets are hidden).
    assert "[redacted]" not in str(out["payload"]["reasoning"])
    assert "[redacted]" not in str(out["payload"]["trajectory"])
    assert out["payload"]["answer"].startswith("71 stations")


def test_project_sse_keeps_expert_output_full_but_redacts_secrets():
    # The expert's extract report (`output`) is content the TUI renders in full, so
    # it must SURVIVE the SSE projection; only genuine secrets stay redacted.
    ev = SemanticEvent(
        event_type="expert.extract.completed",
        session_id="s1",
        trace_id="trace_t1",
        turn_id="t1",
        payload={
            "output": "the full expert report. " * 50,
            "api_key": "sk-secret-should-be-hidden",
        },
    )
    out = project_sse(ev)
    assert out["payload"]["output"].startswith("the full expert report")
    assert "[redacted]" not in str(out["payload"]["output"])
    assert str(out["payload"]["api_key"]).startswith("[redacted]")


def test_project_sse_keeps_reasoning_on_every_event():
    # `reasoning` is the model's chain-of-thought — content from the user's own
    # session, NOT a secret. It now reaches the live UI on EVERY event type (the UI
    # can show the expert's thoughts); it is no longer redacted to a length heartbeat.
    def _ev(event_type):
        return SemanticEvent(
            event_type=event_type,
            session_id="s1",
            trace_id="trace_t1",
            turn_id="t1",
            payload={"reasoning": "deep chain of thought " * 20},
        )

    for et in (
        "react.step.completed",
        "expert.extract.completed",
        "lm.call",
        "lm.token.delta",
        "expert.response.completed",
        "llm.response.completed",
    ):
        assert "[redacted]" not in str(project_sse(_ev(et))["payload"]["reasoning"]), et


def test_project_sse_redacts_genuine_secrets_on_every_event():
    # Genuine credentials are never session content — they stay redacted on SSE.
    def _ev(event_type):
        return SemanticEvent(
            event_type=event_type,
            session_id="s1",
            trace_id="trace_t1",
            turn_id="t1",
            payload={"api_key": "sk-should-be-hidden", "password": "hunter2"},
        )

    for et in ("react.step.completed", "lm.call", "llm.response.completed"):
        out = project_sse(_ev(et))["payload"]
        assert str(out["api_key"]).startswith("[redacted]"), et
        assert str(out["password"]).startswith("[redacted]"), et
    # Full capture is always unredacted regardless of event type.
    assert "[redacted]" not in str(project_full(_ev("lm.call"))["payload"]["api_key"])


def test_project_history_is_deferred():
    with pytest.raises(NotImplementedError):
        project_history(_handoff_event())


def test_default_trace_backend_is_jsonl(tmp_path, monkeypatch):
    # The native JSONL projection is the committed default. Optional providers
    # remain opt-in and ARC remains the semantic-event source.
    monkeypatch.delenv("CLIO_SEMANTIC_TRACE_BACKEND", raising=False)
    monkeypatch.delenv("CLIO_SEMANTIC_TRACE_PATH", raising=False)
    backend = build_trace_backend(tmp_path / "semantic_traces")
    assert backend.name == "jsonl"
    backend.close()


def test_trace_backend_file_legacy_alias_selects_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(tmp_path / "traces"))
    backend = build_trace_backend(tmp_path / "semantic_traces")
    assert backend.name == "jsonl"
    backend.close()


def test_file_backend_dotted_directory_writes_per_session(tmp_path):
    # Regression: a DIRECTORY path containing dots (e.g. a model-named grind dir
    # "trace_..._qwopus3.5-9b-v3_sandiego") must still be treated as a directory of
    # per-session files. Plain Path.suffix truthiness misfired here -> the writer
    # opened a directory as a file and silently dropped every event (empty trace).
    dotted_dir = tmp_path / "trace_lm_studio-qwopus3.5-9b-v3_sandiego_5"
    dotted_dir.mkdir()
    backend = se.FileSemanticTraceBackend(dotted_dir)
    event = SemanticEvent(
        event_type="probe.test",
        session_id="sessABC",
        trace_id="t",
        turn_id="tu",
        actor={},
        payload={"x": 1},
    )
    backend.emit(event)
    backend.flush()
    written = list(dotted_dir.glob("*.semantic.jsonl"))
    assert written == [dotted_dir / "sessABC.semantic.jsonl"], (
        f"expected per-session file in the dotted directory, got {written}"
    )
    assert "probe.test" in written[0].read_text()


def test_file_backend_explicit_jsonl_path_is_single_file(tmp_path):
    target = tmp_path / "all.jsonl"
    backend = se.FileSemanticTraceBackend(target)
    event = SemanticEvent(
        event_type="probe.test",
        session_id="s1",
        trace_id="t",
        turn_id="tu",
        actor={},
        payload={},
    )
    backend.emit(event)
    backend.flush()
    assert target.exists() and "probe.test" in target.read_text()
