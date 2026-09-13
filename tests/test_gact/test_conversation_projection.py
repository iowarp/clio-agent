"""#1339 B.1 -- ``model_context_messages``: the coverage-keyed model-context query.

Pure-function unit tests; no app, no ARC, no LM. See
:mod:`clio_agent.gact.conversation_projection` for the coverage rule.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.conversation_projection import model_context_messages
from clio_agent.gact.types import Message, Part, Tokens

_NOW = "2026-09-11T00:00:00+00:00"


def _text_row(row_id: str, text: str = "hi") -> Message:
    return Message(
        id=row_id,
        session_id="sess_1",
        role="user",
        created_at=_NOW,
        updated_at=_NOW,
        parts=[Part(id=f"part_{row_id}", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def _checkpoint_row(row_id: str, *, summary: str, compacted_message_ids: list[str]) -> Message:
    return Message(
        id=row_id,
        session_id="sess_1",
        role="assistant",
        created_at=_NOW,
        updated_at=_NOW,
        parts=[
            Part(
                id=f"part_{row_id}",
                type="compaction",
                summary=summary,
                compacted_message_ids=compacted_message_ids,
            )
        ],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def test_empty_ledger_returns_empty() -> None:
    assert model_context_messages([]) == []


def test_no_checkpoint_returns_the_whole_list() -> None:
    rows = [_text_row("m1"), _text_row("m2")]
    assert model_context_messages(rows) == rows


def test_one_checkpoint_excludes_only_its_covered_ids() -> None:
    seed = _text_row("msg_seed")
    checkpoint = _checkpoint_row("msg_cp1", summary="s1", compacted_message_ids=["msg_seed"])
    rows = [seed, checkpoint]

    result = model_context_messages(rows)

    assert result == [checkpoint]


def test_two_checkpoints_keeps_latest_plus_uncovered_rows() -> None:
    seed = _text_row("msg_seed")
    cp1 = _checkpoint_row("msg_cp1", summary="s1", compacted_message_ids=["msg_seed"])
    after_first = _text_row("msg_after_first", text="after first checkpoint")
    cp2 = _checkpoint_row(
        "msg_cp2", summary="s2", compacted_message_ids=["msg_cp1", "msg_after_first"]
    )
    rows = [seed, cp1, after_first, cp2]

    result = model_context_messages(rows)

    assert result == [cp2]


def test_dict_rows_are_tolerated() -> None:
    seed = {"id": "msg_seed", "parts": [{"type": "text", "text": "hi"}]}
    checkpoint = {
        "id": "msg_cp1",
        "parts": [{"type": "compaction", "summary": "s1", "compacted_message_ids": ["msg_seed"]}],
    }
    rows: list[Any] = [seed, checkpoint]

    result = model_context_messages(rows)

    assert result == [checkpoint]


def test_the_compacting_turns_own_assistant_row_is_retained() -> None:
    """Turn-boundary placement: the checkpoint lands AFTER the in-flight assistant
    row of the turn that produced it, so a purely positional slice would drop that
    row. Coverage keeps it because the checkpoint's own compacted_message_ids only
    names rows that existed BEFORE the compacting turn started."""

    seed = _text_row("msg_seed")
    user = _text_row("msg_user_2", text="follow-up")
    assistant = _text_row("msg_asst_2", text="the compacting turn's own answer")
    checkpoint = _checkpoint_row(
        "msg_cp1", summary="s1", compacted_message_ids=["msg_seed", "msg_user_2"]
    )
    rows = [seed, user, assistant, checkpoint]

    result = model_context_messages(rows)

    assert result == [assistant, checkpoint]


def test_a_row_covered_only_by_the_first_checkpoint_is_still_excluded() -> None:
    """The second checkpoint's coverage includes the first checkpoint's row id, not
    ``msg_seed`` directly -- the transitive closure must still exclude ``msg_seed``."""

    seed = _text_row("msg_seed")
    cp1 = _checkpoint_row("msg_cp1", summary="s1", compacted_message_ids=["msg_seed"])
    after_first = _text_row("msg_after_first", text="after first checkpoint")
    cp2 = _checkpoint_row(
        "msg_cp2", summary="s2", compacted_message_ids=["msg_cp1", "msg_after_first"]
    )
    rows = [seed, cp1, after_first, cp2]

    result = model_context_messages(rows)

    assert seed not in result
    assert result == [cp2]
