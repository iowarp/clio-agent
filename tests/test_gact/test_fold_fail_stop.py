"""The fold that turns clio-core's records into the model's messages never guesses.

It used to skip kinds it did not model, turn an orphan observation into a user message,
re-match a result to some other call, keep a call with no result, and default a
missing call id or name -- each a silent change to what the model saw. Each is now a
typed ``ContextFoldError`` naming the segment; record kinds that are never agent context
are skipped by declaration.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.clio_react_record import ContextFoldError, fold_steps


def _seg(kind: str, content: Any, sid: str = "") -> Any:
    return SimpleNamespace(kind=kind, content=content, id=sid or f"seg_{kind}")


def _call(cid: str = "c0", name: str = "search") -> Any:
    return _seg("tool_call", {"id": cid, "name": name, "args": {}})


def _obs(cid: str = "c0", text: str = "hits") -> Any:
    return _seg("observation", {"call_id": cid, "text": text, "is_error": False})


def _fails(segments: list[Any], reason: str) -> None:
    with pytest.raises(ContextFoldError) as err:
        fold_steps(segments)
    assert err.value.error_type == "context_fold_failed"
    assert err.value.details["problem"] == reason


def test_a_well_formed_step_folds() -> None:
    messages = fold_steps(
        [_seg("user", {"text": "q"}), _seg("thought", {"text": "t"}), _call(), _obs()]
    )

    assert [m.role for m in messages] == ["user", "assistant", "tool"]


def test_record_kinds_that_are_not_context_are_skipped_by_declaration() -> None:
    messages = fold_steps(
        [_seg("user", {"text": "q"}), _seg("answer", {"text": "a"}), _seg("step_open", {})]
    )

    assert [m.role for m in messages] == ["user"]


def test_an_unmodelled_kind_fails() -> None:
    _fails([_seg("system", {"text": "be terse"})], "unmodelled_kind")


def test_non_mapping_content_fails() -> None:
    _fails([_seg("user", "plain string")], "malformed_content")


def test_an_orphan_observation_fails() -> None:
    _fails([_seg("user", {"text": "q"}), _obs()], "orphan_observation")


def test_a_result_for_an_unknown_call_fails() -> None:
    _fails([_seg("thought", {"text": "t"}), _call("c0"), _obs("c9")], "orphan_observation")


def test_a_call_left_unanswered_fails() -> None:
    _fails(
        [_seg("thought", {"text": "t"}), _call("c0"), _seg("user", {"text": "next"})],
        "unanswered_call",
    )


def test_a_call_with_no_id_or_name_fails() -> None:
    _fails([_seg("thought", {"text": "t"}), _seg("tool_call", {"args": {}})], "malformed_call")


def test_a_summary_with_no_text_fails() -> None:
    _fails([_seg("summary", {})], "empty_summary")


def test_the_escalation_note_is_a_clio_addition_not_an_observation() -> None:
    """The recorder's failed-step note folds as a CLIO addition the model can tell apart."""
    note = {"text": "boom", "source": "turn_escalated", "actor": "algorithm"}

    [message] = fold_steps([_seg("user", note)])

    assert message.parts[0].text == "[clio: turn_escalated]\nboom"
