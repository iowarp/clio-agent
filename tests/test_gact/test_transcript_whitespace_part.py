"""A streamed part is announced on its first non-whitespace chunk (gact-tui#368).

Whitespace-only parts are dropped at close, so announcing one on open left a
live client with a row that never completed and that a reload never shows.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.transcript import TurnTranscript


class RecordingPublisher:
    """Records (event_type, payload) in publish order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        self.events.append((event_type, dict(payload)))

    def of_type(self, *event_types: str) -> list[tuple[str, dict[str, Any]]]:
        return [(t, p) for (t, p) in self.events if t in event_types]


def make_transcript() -> tuple[TurnTranscript, RecordingPublisher]:
    publisher = RecordingPublisher()
    return TurnTranscript(session_id="sess_t", turn_id="turn_t", publisher=publisher), publisher


def _streamed(publisher: RecordingPublisher, part_id: str) -> str:
    return "".join(
        payload["delta"]["text_append"]
        for event_type, payload in publisher.events
        if event_type == "message.part.delta" and payload["part_id"] == part_id
    )


def _kinds(publisher: RecordingPublisher) -> list[tuple[str, Any]]:
    return [
        (event_type, payload.get("part_id") or payload.get("part", {}).get("type"))
        for event_type, payload in publisher.events
        if event_type.startswith("message.part.")
    ]


def test_the_issue_recipe_announces_only_the_answer() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "provider_thinking:anthropic", " ")
    transcript.append_text_delta("main", "provider_thinking:anthropic", "\n")
    transcript.append_text_delta("main", "answer", "The answer is 42.")
    transcript.close_open_text()

    (snapshot,) = transcript.snapshot()
    assert snapshot.type == "text"
    assert _kinds(publisher) == [
        ("message.part.added", "text"),
        ("message.part.delta", snapshot.id),
        ("message.part.completed", snapshot.id),
    ]


def test_leading_whitespace_is_held_and_sent_with_the_first_content() -> None:
    transcript, publisher = make_transcript()
    for chunk in ["\n", "  ", "Hello", " ", "world", "\n"]:
        transcript.append_text_delta("main", "answer", chunk)
    transcript.close_open_text()

    (part,) = transcript.snapshot()
    deltas = [p["delta"]["text_append"] for _, p in publisher.of_type("message.part.delta")]
    assert deltas == ["\n  Hello", " ", "world", "\n"]
    ((_, completed),) = publisher.of_type("message.part.completed")
    # Live (deltas) == completed == reload (ledger), byte for byte.
    assert (
        _streamed(publisher, part.id) == completed["final_text"] == part.text == "\n  Hello world\n"
    )


def test_whitespace_thinking_before_a_tool_call_leaves_no_trace() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "provider_thinking:anthropic", "\n\n")
    transcript.close_open_text()
    transcript.append_text_delta("main", "provider_thinking:anthropic", "Real reasoning")
    transcript.close_open_text()

    (part,) = transcript.snapshot()
    assert part.type == "thinking" and part.text == "Real reasoning"
    added = publisher.of_type("message.part.added")
    assert [payload["part"]["id"] for _, payload in added] == [part.id]
