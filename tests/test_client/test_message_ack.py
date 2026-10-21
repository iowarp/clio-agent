"""Pure client acknowledgement compatibility; no server or native Core needed."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from clio_agent.client import PostMessageAck


@pytest.mark.parametrize("fields", [{}, {"task_handle": None}, {"task_handle": "run_accepted"}])
def test_ack_keeps_optional_run_handle(fields: dict[str, Any]) -> None:
    """Queued feedback has no separate run; legacy acknowledgements omit the field."""
    ack = PostMessageAck.model_validate({"message_id": "msg_user", **fields})
    assert ack.task_handle == fields.get("task_handle")


def test_ack_rejects_non_string_handle() -> None:
    """An optional handle still rejects malformed wire values."""
    with pytest.raises(ValidationError, match="task_handle"):
        PostMessageAck.model_validate({"message_id": "msg_user", "task_handle": 7})
