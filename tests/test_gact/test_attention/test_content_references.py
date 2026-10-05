"""Whole-part selection keeps media and tool identities without fabricated coordinates."""

from __future__ import annotations

from clio_agent.gact.attention.content_references import message_content_references
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message


def test_tool_fields_keep_call_and_result_ids_and_media_unavailability() -> None:
    message = Message(
        id="m",
        session_id="s",
        role="assistant",
        created_at="",
        updated_at="",
        parts=[
            Part(
                id="call", type="tool_call", call_id="tool-1", thought="Think 🦉", input={"x": "é"}
            ),
            Part(
                id="result",
                type="tool_result",
                call_id="tool-1",
                text="Evidence",
                content=[Part(type="image", data="private image")],
            ),
            Part(id="artifact", type="resource_link", uri="artifact://a/revision/3"),
            Part(id="surface", type="a2ui", surface_id="surface-1"),
        ],
    )
    rows = message_content_references(message)
    refs = {(row["reference"]["part_id"], row["reference"]["field"]): row for row in rows}
    assert refs["call", "thought"]["characters"] == 7
    assert refs["call", "input"]["reference"]["call_id"] == "tool-1"
    assert refs["result", "result"]["coordinate_support"] == "text"
    assert refs["result", "content"]["coordinate_support"] == "unavailable"
    assert refs["artifact", "content"]["reference"]["artifact_ref"] == "artifact://a/revision/3"
    assert refs["surface", "content"]["coordinate_support"] == "unavailable"
    assert "private image" not in str(rows)
    before = refs["result", "content"]["reference"]["content_revision"]
    message.parts[1].content[0].data = "changed private image"
    updated = message_content_references(message)
    assert (
        next(
            row
            for row in updated
            if row["reference"]["part_id"] == "result" and row["reference"]["field"] == "content"
        )["reference"]["content_revision"]
        != before
    )
