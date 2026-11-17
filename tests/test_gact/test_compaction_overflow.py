"""Recovering from a request too long for the model: what the summary reads and keeps."""

from __future__ import annotations

from typing import Any

from dspy.lm15 import Message, TextPart, ToolCallPart, ToolResultPart

from clio_agent.gact import compaction_overflow
from clio_agent.gact.compaction_policy import keep_policy, overflow_keep_policy
from clio_agent.gact.compaction_summarizer import clip_tool_results


def test_the_summary_copy_cuts_long_tool_results_and_says_so() -> None:
    long = "x" * 10_000
    messages = (
        Message(role="user", parts=(TextPart(text="find the data " * 50),)),
        Message(role="assistant", parts=(ToolCallPart(id="c1", name="search", input={}),)),
        Message(
            role="tool",
            parts=(ToolResultPart(id="c1", name="search", content=(TextPart(text=long),)),),
        ),
    )
    clipped = clip_tool_results(messages, 100)
    result = clipped[2].parts[0].content[0].text
    assert result.startswith("x" * 100) and "[cut for this summary: 10,000 characters" in result
    assert clipped[0] == messages[0] and clipped[1] == messages[1]  # only tool results
    assert messages[2].parts[0].content[0].text == long  # the context itself is unchanged


def test_overflow_recovery_keeps_no_step_verbatim() -> None:
    assert keep_policy().keep_last_steps >= 1
    with overflow_keep_policy():
        policy = keep_policy()
        assert policy.keep_last_steps == 0 and policy.keep_last_turns == 0
        assert policy.keep_head  # the person's request still comes back
    assert keep_policy().keep_last_steps >= 1


def test_without_a_context_store_it_reports_and_does_not_compact(
    monkeypatch: Any,
) -> None:
    reported: list[tuple[str, dict[str, Any]]] = []
    def audit(stage: str, /, **fields: Any) -> None:
        reported.append((stage, fields))

    monkeypatch.setattr(compaction_overflow, "stream_audit", audit)
    monkeypatch.setattr(
        "clio_agent.gact.agents.clio_react_record.arc_scope", lambda: (None, "sess_x", "main")
    )
    assert compaction_overflow.compact_after_overflow(object(), object()) is False
    assert reported == [
        (
            compaction_overflow.OVERFLOW_NOT_COMPACTED,
            {"session_id": "sess_x", "reason": "no_context_store"},
        )
    ]
