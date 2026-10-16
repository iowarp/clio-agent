"""Reasoning phase timing survives persistence and never includes tool execution."""

from typing import Any

from clio_agent.gact import reasoning_timing as timing
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import _message_block_completed
from clio_agent.gact.protocol.v3.message import message_to_v3
from tests.test_gact.test_turn_transcript import make_transcript, tool_part


def test_reasoning_closes_before_tool_work_and_live_timing_matches_reload(monkeypatch: Any) -> None:
    clock = [100.0]
    monkeypatch.setattr(timing.time, "perf_counter", lambda: clock[0])
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "provider_thinking:model:provider_field", "Inspecting")
    clock[0] += 12.4
    transcript.append_text_delta("main", "provider_thinking:model:provider_field", " the file.")
    transcript.append_part(tool_part("call"))
    clock[0] += 33.9  # Tool work does not extend a closed reasoning passage.
    transcript.close_open_text()
    parts = transcript.snapshot()
    thought = next(part for part in parts if part.type == "thinking")
    assert round(thought.duration_ms or 0) == 12_400
    assert thought.metadata["started_at"] and thought.metadata["completed_at"]
    completed = next(
        payload for event, payload in publisher.events if event == "message.part.completed"
    )
    projected = _message_block_completed(
        Event(type="message.part.completed", session_id="sess_t", payload=completed),
        completed,
        None,
    )
    reloaded = message_to_v3(
        {
            "id": transcript.message_id,
            "session_id": "sess_t",
            "role": "assistant",
            "parts": [part.to_wire() for part in parts],
        }
    )
    assert projected.payload["block"] == reloaded["blocks"][0]
    assert round(projected.payload["block"]["duration_ms"]) == 12_400
    assert "duration_ms" not in reloaded["blocks"][1]


def test_batch_reasoning_without_a_real_open_boundary_has_no_invented_timing() -> None:
    from clio_agent.gact.types import Part

    transcript, _ = make_transcript()
    transcript.append_part(Part(id="historical", type="thinking", text="Earlier reasoning."))
    thought = transcript.snapshot()[0]
    assert "started_at" not in thought.metadata
    assert "duration_ms" not in timing.reasoning_timing(thought.to_wire())


def test_new_passage_and_clock_skew_do_not_reuse_an_old_duration(monkeypatch: Any) -> None:
    clock = [50.0]
    monkeypatch.setattr(timing.time, "perf_counter", lambda: clock[0])
    transcript, _ = make_transcript()
    transcript.append_text_delta("main", "provider_thinking:model:provider_field", "first")
    clock[0] = 60.0
    transcript.close_open_text()
    transcript.append_text_delta("main", "provider_thinking:model:provider_field", "second")
    clock[0] = 62.0
    transcript.close_open_text()
    assert [round(part.duration_ms or 0) for part in transcript.snapshot()] == [10_000, 2_000]
