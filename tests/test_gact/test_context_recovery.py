"""Lossless restart recovery, compaction/rollback isolation and exact receipts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.history_plane import HistoryPlane
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context
from clio_agent.gact.agents.clio_react_record import fold_steps
from clio_agent.gact.agents.context_recovery import (
    recorded_context,
    recovery_details,
    transcript_context,
)
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.semantic_trace_file import FileSemanticTraceBackend, TraceWriteError


@pytest.mark.parametrize("dispatched", [False, True])
def test_restart_restores_actual_inputs_results_and_skill_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatched: bool
) -> None:
    from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher
    from clio_agent.gact.provenance.jsonl import JsonlProvenanceProvider

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    backend = (
        ProvenanceDispatcher([JsonlProvenanceProvider(tmp_path / "trace")])
        if dispatched
        else FileSemanticTraceBackend(tmp_path / "trace")
    )

    def record(op: str, session: str, scope: str, **payload: Any) -> dict[str, Any]:
        event = SemanticEvent(
            event_type="arc.op",
            session_id=session,
            trace_id="t",
            turn_id="u1",
            payload={"op": op, "scope": scope, **payload},
        )
        backend.emit(event)
        return event.to_dict("full")

    arc.set_segment_op_logger(record)
    for kind, content in [
        ("user", {"text": "Read the data"}),
        (
            "thought",
            {
                "text": "Inspecting",
                "thinking": [
                    {
                        "text": "Recorded reasoning",
                        "continuation": [
                            {"provider": "test", "kind": "signature", "data": {"state": "exact"}}
                        ],
                    }
                ],
            },
        ),
        (
            "tool_call",
            {"id": "c", "name": "load_skill", "args": {"skill_id": "installed-procedure"}},
        ),
        (
            "observation",
            {"call_id": "c", "text": "# Complete skill\nOriginal instructions", "is_error": False},
        ),
        ("thought", {"text": "Read complete"}),
    ]:
        arc.append_segment("s", "main", kind, content, turn_id="u1")
    expected = arc.render_working_set("s", "main")
    app = SimpleNamespace(state=SimpleNamespace(semantic_trace_backend=backend))
    monkeypatch.setattr(context, "active_app", lambda: app)
    recovered = recorded_context("s", "main", [{"id": "u1", "role": "user"}])
    assert [s.id for s in recovered] == [s.id for s in expected]
    assert [s.content for s in recovered] == [s.content for s in expected]
    # A different process plane has no context, but the trace retains all records.
    cold = HistoryPlane()
    assert cold.restore_empty_context("s", "main", recovered)
    assert fold_steps(cold.render_working_set("s", "main")) == fold_steps(expected)
    assert not cold.restore_empty_context("s", "main", recovered)
    receipt = recovery_details("Recovered", recovered)
    assert "Original instructions" in receipt and '"skill_id": "installed-procedure"' in receipt
    backend.close()


def test_recovered_core_lane_survives_a_cold_read(tmp_path: Path) -> None:
    source = HistoryPlane()
    source.append_segment("s", "main", "user", {"text": "q"}, turn_id="u1")
    removed = source.append_segment("s", "main", "thought", {"text": "retired"}, turn_id="u1")
    source.delete_segments("s", "main", [removed.id])
    source.append_segment("s", "main", "thought", {"text": "replacement"}, turn_id="u1")
    snapshots = source.list_segments("s", "main", include_tombstoned=True)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    assert arc.restore_empty_context("s", "main", snapshots)
    assert [s.content["text"] for s in arc.render_working_set("s", "main")] == ["q", "replacement"]
    arc.release_session("s")
    assert [s.content["text"] for s in arc.render_working_set("s", "main")] == ["q", "replacement"]
    assert not arc.restore_empty_context("s", "main", snapshots)


def test_trace_corruption_is_loud(tmp_path: Path) -> None:
    backend = FileSemanticTraceBackend(tmp_path / "trace")
    backend.path.mkdir()
    (backend.path / "s.semantic.jsonl").write_text('{"event_type":', encoding="utf-8")
    with pytest.raises(TraceWriteError, match="Unreadable event"):
        list(backend.session_events("s"))


def test_later_restoration_replaces_an_earlier_plane_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msgspec

    backend = FileSemanticTraceBackend(tmp_path / "trace")
    for op, identity, value in [("append", "old", "old content"), ("restore", "new", "restored")]:
        segments = transcript_context(
            "s", "main", [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": value}]}]
        )
        segment = msgspec.structs.replace(segments[0], id=identity)
        backend.emit(
            SemanticEvent(
                event_type="arc.op",
                session_id="s",
                trace_id="t",
                turn_id="u1",
                payload={
                    "op": op,
                    "scope": "main",
                    "segments_written": [msgspec.to_builtins(segment)],
                },
            )
        )
    monkeypatch.setattr(
        context,
        "active_app",
        lambda: SimpleNamespace(state=SimpleNamespace(semantic_trace_backend=backend)),
    )
    recovered = recorded_context("s", "main", [{"id": "u1", "role": "user"}])
    assert [segment.id for segment in recovered] == ["new"]
    assert recovered[0].content["text"] == "restored"


def test_transcript_reconstruction_keeps_tools_in_place_and_original_turn_id() -> None:
    rows = [
        {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "Inspect"}]},
        {
            "id": "a1",
            "role": "assistant",
            "metadata": {"tools_called": [{"name": "shell_bash", "args": {"command": "ls data"}}]},
            "parts": [
                {"type": "thinking", "text": "Checking"},
                {"type": "tool_call", "call_id": "c", "tool_name": "shell_bash"},
                {
                    "type": "tool_result",
                    "call_id": "c",
                    "content": [{"type": "text", "text": "file.csv\n"}],
                },
                {"type": "text", "text": "One file"},
            ],
        },
    ]
    segments = transcript_context("s", "main", rows)
    assert all(s.turn_id == "u1" for s in segments)
    messages = fold_steps(segments)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].parts[-1].input == {"command": "ls data"}
    assert messages[2].parts[0].content[0].text == "file.csv\n"


def test_unanswered_transcript_call_never_seeds_partial_context() -> None:
    with pytest.raises(ValueError, match="unanswered"):
        transcript_context(
            "s",
            "main",
            [
                {
                    "role": "assistant",
                    "parts": [
                        {
                            "type": "tool_call",
                            "call_id": "c",
                            "tool_name": "read",
                            "input": {"path": "x"},
                        }
                    ],
                }
            ],
        )
