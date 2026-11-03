"""``lm.call`` records store each chain's new messages; readers rebuild the rest."""

from __future__ import annotations

import json
import uuid
from typing import Any

from clio_agent.lm.call_messages_delta import delta, rebuild


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "parts": [{"type": "text", "text": text}]}


def _record(session: str, messages: list[Any]) -> dict[str, Any]:
    stored, prefix = delta(session, messages)
    return {"messages": stored, **({"messages_prefix": prefix} if prefix else {})}


def _turn(steps: int) -> list[list[Any]]:
    """The requests of one growing ReAct turn: system, question, then a step per call."""
    history = [_msg("system", "sys " * 50), _msg("user", "investigate")]
    requests = []
    for step in range(steps):
        requests.append(list(history))
        history += [_msg("assistant", f"call {step}"), _msg("tool", f"result {step} " * 40)]
    return requests


def test_a_growing_turn_stores_each_message_once_and_rebuilds_exactly() -> None:
    """SABOTAGE: return ``messages, {}`` from ``_Chains.delta`` and each record stores the
    whole conversation again (the stored size grows with the square of the steps)."""
    session = f"sess_{uuid.uuid4().hex[:8]}"
    requests = _turn(30)
    records = [_record(session, request) for request in requests]
    assert "messages_prefix" not in records[0]
    assert all("messages_prefix" in record for record in records[1:])
    stored = sum(len(json.dumps(record["messages"])) for record in records)
    whole = sum(len(json.dumps(request)) for request in requests)
    assert stored <= len(json.dumps(requests[-1])) + 1000  # about one copy of the last
    assert stored < whole / 10
    assert rebuild(records) == requests


def test_a_rewritten_head_and_an_interleaved_chain_are_recorded_whole_or_apart() -> None:
    """A compaction rewrites the conversation: recorded whole. A summarizer call in the
    same session (another first message) has its own chain and breaks nothing."""
    session = f"sess_{uuid.uuid4().hex[:8]}"
    main = _turn(4)
    summarizer = [_msg("system", "summarize"), _msg("user", "the history")]
    compacted = [main[0][0], _msg("user", "summary of the work so far"), _msg("user", "go on")]
    after = [*compacted, _msg("assistant", "call 9"), _msg("tool", "result 9")]
    requests = [*main[:2], summarizer, main[2], main[3], compacted, after]
    records = [_record(session, request) for request in requests]
    assert "messages_prefix" not in records[2]  # the summarizer: its own chain, whole
    assert records[3]["messages_prefix"]["count"] == len(main[1])  # the main chain held
    assert "messages_prefix" not in records[5]  # the compacted head: whole
    assert records[6]["messages_prefix"]["count"] == len(compacted)
    assert rebuild(records) == requests


def test_a_missing_earlier_record_makes_its_dependents_unavailable_never_wrong() -> None:
    """SABOTAGE: drop the ``len(known[0]) < count`` check in ``rebuild`` and the record
    after the gap is rebuilt on a prefix the reader does not hold."""
    session = f"sess_{uuid.uuid4().hex[:8]}"
    requests = _turn(4)
    records = [_record(session, request) for request in requests]
    del records[2]  # a lost record: the next one extends what the reader never saw
    rebuilt = rebuild(records)
    assert rebuilt[:2] == requests[:2]
    assert rebuilt[2] is None


def test_no_session_or_no_messages_is_recorded_as_given() -> None:
    assert delta("", [_msg("user", "q")]) == ([_msg("user", "q")], {})
    assert delta("sess_x", None) == (None, {})
    assert delta("sess_x", []) == ([], {})


def test_a_lost_compaction_record_never_rebuilds_on_the_old_conversation() -> None:
    """The record that rewrote the head is lost; the next one extends the rewritten head.
    The reader's chain (the conversation before compaction) is longer than the prefix,
    so only the digest tells them apart.

    SABOTAGE: drop the digest comparison in ``rebuild`` and the record is rebuilt on the
    pre-compaction messages.
    """
    session = f"sess_{uuid.uuid4().hex[:8]}"
    main = _turn(6)
    compacted = [main[0][0], _msg("user", "summary"), _msg("user", "go on")]
    after = [*compacted, _msg("assistant", "next")]
    records = [_record(session, request) for request in [*main, compacted, after]]
    del records[len(main)]  # the compaction's whole record is lost
    rebuilt = rebuild(records)
    assert rebuilt[: len(main)] == main
    assert rebuilt[len(main)] is None
