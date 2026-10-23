"""Result-page cursors stay short enough for a model to copy (#1844)."""

from __future__ import annotations

import pytest

from clio_agent.arc.task_types import CoreReference
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_cursors import encode_cursor
from clio_agent.gact.task_result_content import SERIALIZATION, read_cursor, result_cursor

DIGEST = "ab" * 32
SESSION = "sess_" + "0" * 32
HANDLE = "task_" + "1" * 32
REFERENCE = CoreReference(
    session_id=SESSION,
    scope=f"_events/t/content/{DIGEST}",
    content_id=DIGEST,
    sha256=DIGEST,
)
CALLER = TaskCaller(SESSION, "human")


def test_a_cursor_carries_tags_instead_of_the_whole_reference() -> None:
    cursor = result_cursor(SESSION, HANDLE, REFERENCE, "task_result", 36000)
    assert len(cursor) <= 64
    assert DIGEST not in cursor and SESSION not in cursor and HANDLE not in cursor
    position = read_cursor(cursor, CALLER, HANDLE)
    assert position["mode"] == "task_result" and position["offset"] == 36000


def test_the_reference_pin_survives_the_short_form() -> None:
    changed = REFERENCE.model_copy(update={"content_id": "cd" * 32, "sha256": "cd" * 32})
    same = read_cursor(result_cursor(SESSION, HANDLE, REFERENCE, "task_result"), CALLER, HANDLE)
    other = read_cursor(result_cursor(SESSION, HANDLE, changed, "task_result"), CALLER, HANDLE)
    assert same["content"] != other["content"]


@pytest.mark.parametrize(
    ("caller", "handle", "edit"),
    [
        (TaskCaller("sess_other", "human"), HANDLE, None),
        (CALLER, "task_other", None),
        (CALLER, HANDLE, ("task_result", "shared_evidence")),
        (CALLER, HANDLE, ("36000", "-1")),
        (CALLER, HANDLE, ("36000", "36000.0")),
    ],
    ids=["other-session", "other-handle", "switched-mode", "negative-offset", "extra-part"],
)
def test_a_cursor_for_a_different_read_is_refused(
    caller: TaskCaller, handle: str, edit: tuple[str, str] | None
) -> None:
    cursor = result_cursor(SESSION, HANDLE, REFERENCE, "task_result", 36000)
    if edit is not None:
        cursor = cursor.replace(*edit, 1)
    with pytest.raises(ValueError, match="different read"):
        read_cursor(cursor, caller, handle)


def test_a_version_1_cursor_still_reads_the_same_position() -> None:
    legacy = encode_cursor(
        {
            "version": 1,
            "serialization": SERIALIZATION,
            "recipient": SESSION,
            "handle": HANDLE,
            "mode": "shared_evidence",
            "reference": REFERENCE.model_dump(mode="json"),
            "offset": 24000,
        }
    )
    short = result_cursor(SESSION, HANDLE, REFERENCE, "shared_evidence", 24000)
    assert len(legacy) > 600
    assert read_cursor(legacy, CALLER, HANDLE) == read_cursor(short, CALLER, HANDLE)
    with pytest.raises(ValueError, match="different read"):
        read_cursor(legacy, TaskCaller("sess_other", "human"), HANDLE)
