"""The compaction summarizer continues the agent's own request, so it reuses its cache.

The H5 compactions (2026-11-03) read 0 and 789 cached tokens against 33k to 158k on the
agent steps around them: the summarizer was a new prompt. Now its request is the
loop's next request plus the compaction prompt, with the same model, system, tools and
config, so a provider that keeps the conversation (``claude_code``, Codex) is sent only
the new messages, and a prefix cache (Anthropic, OpenAI, vLLM, LM Studio) serves the
rest.

SABOTAGE (each turns the named test red):

* ``loop_summarizer``: send ``tools=()`` ->
  ``test_the_request_is_the_loops_next_request_plus_the_prompt``.
* ``loop_summarizer``: send another ``system`` -> both tests.
* ``loop_summarizer``: rebuild the history instead of extending ``next_request()``
  (for instance drop the last message) -> both tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import Config, FunctionTool, Message, Request, ToolChoice

from clio_agent.gact.compaction_summarizer import loop_summarizer
from clio_agent.providers import claude_code_conversation
from tests._compaction_summary import SummaryLM, request_text, split_seen

_TOOL = FunctionTool(name="search", description="find", parameters={"type": "object"})


def _base() -> Request:
    return Request(
        model="m",
        system="you are the agent",
        messages=(
            Message.user("find the stations"),
            Message.assistant("searching"),
            Message.user("HITS-1"),
        ),
        tools=(_TOOL,),
        config=Config(max_tokens=123),
    )


def test_the_request_is_the_loops_next_request_plus_the_prompt() -> None:
    lm = SummaryLM(lambda seen: "CHECKPOINT")
    summarize = loop_summarizer(lambda: lm, _base)
    assert summarize("write the checkpoint") == "CHECKPOINT"
    [sent] = lm.requests
    base = _base()
    assert (sent.model, sent.system, sent.tools, sent.config) == (
        base.model,
        base.system,
        base.tools,
        base.config,
    )
    assert sent.messages[:-1] == base.messages  # the agent's context, unchanged
    history, prompt = split_seen(request_text(sent))
    assert "find the stations" in history and prompt == "write the checkpoint"


def test_a_kept_conversation_is_continued_with_only_the_new_messages() -> None:
    """The conversation a Claude Code client holds: the agent's last request was
    answered ("searching"), then a tool result arrived; the summarizer's request
    continues that conversation, sending only the tool result and the prompt."""
    entry = SimpleNamespace(conversation=None, pending_reset=None)
    identity = ("m", "/w", "")
    first = Request(
        model="m",
        system="you are the agent",
        messages=(Message.user("find the stations"),),
        tools=(_TOOL,),
        config=Config(max_tokens=123),
    )
    opened = claude_code_conversation.plan(entry, first, "you are the agent", identity, kept=True)
    assert opened.reason == "first_call"
    claude_code_conversation.record_sent(
        entry, opened, "conversation-1", first, "you are the agent"
    )

    lm = SummaryLM(lambda seen: "CHECKPOINT")
    loop_summarizer(lambda: lm, _base)("write the checkpoint")
    [sent] = lm.requests
    plan = claude_code_conversation.plan(entry, sent, "you are the agent", identity, kept=True)
    assert (plan.handle, plan.reason) == ("conversation-1", None)
    texts = [_text(m) for m in plan.messages]
    assert texts == ["HITS-1", "write the checkpoint"]


def _text(message: Message) -> str:
    return "".join(getattr(part, "text", "") for part in message.parts)


class _ToolCallFirstLM:
    """Answers the first request with only a tool call, then with ``text``."""

    model = "test/summary"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[Request] = []

    async def acall(self, request: Request) -> Any:
        from dspy.lm15 import Response, TextPart, ToolCallPart, Usage

        self.requests.append(request)
        part: Any = (
            ToolCallPart(id="c1", name="search", input={"q": "more"})
            if len(self.requests) == 1
            else TextPart(text=self.text)
        )
        return Response(
            id=None,
            model=self.model,
            message=Message(role="assistant", parts=(part,)),
            finish_reason="tool_call" if len(self.requests) == 1 else "stop",
            usage=Usage(),
        )


def test_a_reply_that_is_only_a_tool_call_is_asked_again_with_tool_use_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summarizer request declares the agent's tools (cache reuse), so a model may
    answer with a call and no text: that was an ``empty_summary`` that failed the turn.
    It is asked once more with ``tool_choice`` none, the tools still declared, and the
    retry is traced typed.

    SABOTAGE: return the first reply's text as is -> the summary is empty."""
    from clio_agent.gact import compaction_summarizer
    from clio_agent.runtime import trace

    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda tag, msg, *args: events.append(msg % args))
    lm = _ToolCallFirstLM("CHECKPOINT")
    assert loop_summarizer(lambda: lm, _base)("write the checkpoint") == "CHECKPOINT"
    first, retry = lm.requests
    assert first.config.tool_choice is None and retry.tools == first.tools
    assert retry.config.tool_choice == ToolChoice(mode="none")
    assert retry.config.max_tokens == 123 and retry.messages == first.messages
    assert any(compaction_summarizer.TOOL_CALL_REASON in e for e in events)
