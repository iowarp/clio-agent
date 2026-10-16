"""Goal notices retain command identity and share live/reload projection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.command_presentation import command_result_part
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import _message_block_upsert
from clio_agent.gact.protocol.v3.message import message_to_v3, part_to_v3_block


def _app() -> Any:
    goal = {"condition": "Finish the report", "active": True, "goal_id": "g1"}
    return SimpleNamespace(
        state=SimpleNamespace(sessions={"s": SimpleNamespace(metadata={"goal": goal})})
    )


@pytest.mark.parametrize(
    ("body", "code", "text"),
    [
        (
            "Goal started. Open Work to inspect or stop it. Zero limits mean unlimited.",
            "goal_started",
            "Finish the report",
        ),
        ("Goal cleared.", "goal_stopped", "Finish the report"),
        (
            "/goal rejected: Invalid time limit (reason=goal_invalid_limit)",
            "goal_rejected",
            "Invalid time limit (reason=goal_invalid_limit)",
        ),
        ("There is no active goal to clear.", "goal_command", "There is no active goal to clear."),
    ],
)
def test_new_goal_command_has_an_explicit_notice_instead_of_answer_prose(
    body: str, code: str, text: str
) -> None:
    part = command_result_part(_app(), "s", "/goal", body)
    assert part.type == "notice"
    assert part.source == "goal"
    assert part.code == code
    assert part.text == text
    block = part_to_v3_block(part.to_wire())
    assert block["type"] == "notice"
    assert block["source"] == "goal"
    assert "[/goal]" not in block["text"]
    message = message_to_v3(
        {"id": "m", "role": "assistant", "session_id": "s", "parts": [part.to_wire()]}
    )
    assert message["blocks"] == [block]
    event = Event(
        type="message.part.added",
        session_id="s",
        payload={"message_id": "m", "part": part.to_wire()},
    )
    projected = _message_block_upsert(event, event.payload, None)
    assert projected is not None
    assert projected.payload["block"] == block


def test_old_goal_command_is_upgraded_without_rewriting_the_record() -> None:
    old = {
        "id": "old",
        "type": "text",
        "text": "[/goal] Goal started. Open Work to inspect or stop it. Zero limits mean unlimited.",
        "metadata": {"synthetic": "command_result", "command": "/goal"},
    }
    block = part_to_v3_block(old)
    assert block == {
        "id": "old",
        "type": "notice",
        "source": "goal",
        "code": "goal_started",
        "text": "",
    }
    assert old["type"] == "text"
    assert old["text"].startswith("[/goal]")


def test_ordinary_prose_and_other_commands_keep_their_semantics() -> None:
    ordinary = {"id": "p", "type": "text", "text": "[/goal] is a command example."}
    assert part_to_v3_block(ordinary)["type"] == "text"
    other = command_result_part(None, "s", "/cache-stats", "42 records")
    assert other.type == "text"
    assert other.text == "[/cache-stats] 42 records"
