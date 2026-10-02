"""A part changed after it streamed reaches a live v3 client as the whole block.

Found in the web UI check: a tool-free final step streams as ``next_thought`` and is
promoted to the answer when the turn ends; the promotion published only a metadata
patch, which the v3 projection turned into an empty block, so the live view kept the
answer as an activity row until a reload.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.direct_response import promote_tool_free_response
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3 import event as v3_event
from clio_agent.gact.transcript import TurnTranscript


class _Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))


def _transcript(publisher: _Publisher) -> TurnTranscript:
    return TurnTranscript(session_id="s1", turn_id="t1", publisher=publisher)


def test_the_promoted_answer_reaches_a_live_client_as_the_answer_block() -> None:
    publisher = _Publisher()
    transcript = _transcript(publisher)
    transcript.append_text_delta("main", "next_thought", "The answer.")
    promoted = promote_tool_free_response(
        transcript, SimpleNamespace(termination_reason="direct_response"), ["main"]
    )
    assert promoted
    kind, payload = publisher.events[-1]
    assert kind == "message.part.updated"
    projected = v3_event._message_block_upsert(
        Event(type=kind, session_id="s1", payload=payload), payload, None
    )
    assert projected is not None
    block = projected.payload["block"]
    assert (block["type"], block["text"], block["channel"]) == ("text", "The answer.", "answer")


def test_a_patch_only_update_projects_no_phantom_block() -> None:
    payload = {"message_id": "msg_1", "part_id": "p1", "metadata_patch": {"x": 1}}
    event = Event(type="message.part.updated", session_id="s1", payload=payload)
    assert v3_event._message_block_upsert(event, payload, None) is None
