"""Public transcript model attribution comes only from the recorded route."""

from typing import Any

import pytest

from clio_agent.gact.events import Event
from clio_agent.gact.protocol_v3 import event_to_v3, message_to_v3
from clio_agent.gact.types import Message


def recorded_message(model: Any) -> Message:
    """Make a persisted turn with private metadata beside its public route."""
    return Message(
        id="prompt_1",
        session_id="session_1",
        turn_id="prompt_1",
        role="user",
        created_at="2026-10-06T12:00:00Z",
        updated_at="2026-10-06T12:00:00Z",
        metadata={"effective_model": model, "private_runtime": "never expose"},
    )


def test_message_model_projects_only_recorded_identity() -> None:
    row = message_to_v3(
        recorded_message(
            {
                "provider_id": "codex",
                "model_id": "gpt-6-luna",
                "private_key": "never expose",
            }
        )
    )
    assert row["model"] == {"provider_id": "codex", "model_id": "gpt-6-luna"}
    assert "metadata" not in row
    assert "never expose" not in str(row)


@pytest.mark.parametrize(
    "model",
    [
        None,
        "codex",
        {},
        {"provider_id": "codex"},
        {"provider_id": 1, "model_id": "luna"},
        {"provider_id": "codex", "model_id": ""},
    ],
    ids=["absent", "text", "empty", "missing-model", "invalid-provider", "empty-model"],
)
def test_message_model_does_not_guess_missing_or_malformed_identity(model: Any) -> None:
    assert "model" not in message_to_v3(recorded_message(model))


def test_live_message_event_preserves_the_same_recorded_model() -> None:
    message = recorded_message({"provider_id": "claude_code", "model_id": "claude-sonnet-5"})
    event = event_to_v3(
        Event(type="message.created", session_id=message.session_id, payload=message.to_wire())
    )
    assert event is not None
    assert event["payload"]["model"] == message_to_v3(message)["model"]
    accepted = event_to_v3(
        Event(
            type="message.accepted",
            session_id=message.session_id,
            payload={"message": message.to_wire(), "delivery": "start", "state": "started"},
        )
    )
    assert accepted is not None
    assert accepted["payload"]["message"]["model"] == message_to_v3(message)["model"]
