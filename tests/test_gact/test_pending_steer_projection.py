"""``metadata.pending_steer`` follows the pending-input index, as ``GET .../pending-steers`` does."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.session_store import (
    pending_input_message_ids,
    project_committed_feedback,
)


class _History:
    def __init__(self, items: list[Any], contents: dict[str, Any]) -> None:
        self.items, self.contents = items, contents

    def pending_mail(self, session_id: str) -> tuple[Any, ...]:
        return tuple(self.items)

    def read_content(self, reference: str) -> Any:
        return self.contents[reference]


def _message(identity: str) -> Any:
    return SimpleNamespace(id=identity, metadata={"pending_steer": True, "delivery": "steer"})


def _item(identity: str, kind: str, delivery: str = "eligible") -> Any:
    return SimpleNamespace(id=identity, kind=kind, delivery=delivery, content="c:" + identity)


def test_a_promoted_queued_message_is_no_longer_pending_once_given_to_the_model() -> None:
    # The mailbox keeps the queue item's id; the message carries client_message_id.
    message = _message("crash-1-abc")
    project_committed_feedback(_History([], {}), "sess_1", [message])
    assert message.metadata["pending_steer"] is False
    assert message.metadata["delivery"] == "steer"  # nothing else is rewritten


def test_a_promoted_message_still_pending_keeps_its_flag_by_its_own_id() -> None:
    message = _message("crash-1-abc")
    history = _History(
        [_item("qmsg_1", "composer")], {"c:qmsg_1": {"message_id": "crash-1-abc", "text": "x"}}
    )
    assert pending_input_message_ids(history, "sess_1") == {"crash-1-abc"}
    project_committed_feedback(history, "sess_1", [message])
    assert message.metadata["pending_steer"] is True


def test_only_person_inputs_not_still_queued_count_as_pending() -> None:
    history = _History(
        [
            _item("user_1", "human"),
            _item("qmsg_2", "composer", delivery="queued"),
            _item("task_result:x", "task_result"),
        ],
        {},
    )
    assert pending_input_message_ids(history, "sess_1") == {"user_1"}
    pending, delivered = _message("user_1"), _message("user_0")
    project_committed_feedback(history, "sess_1", [pending, delivered])
    assert pending.metadata["pending_steer"] is True
    assert delivered.metadata["pending_steer"] is False
