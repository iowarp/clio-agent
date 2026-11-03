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

from dspy.lm15 import Config, FunctionTool, Message, Request

from clio_agent.gact.compaction_summarizer import loop_summarizer
from clio_agent.lm.engines.conversations import ConversationRegistry
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
    """The registry the stateful engines share: the agent's last request was answered
    ("searching"), then a tool result arrived; the summarizer's request continues that
    conversation, sending only the tool result and the prompt."""
    registry = ConversationRegistry(lambda: 8)
    key = ("sess", "scope", "m")
    first = Request(
        model="m",
        system="you are the agent",
        messages=(Message.user("find the stations"),),
        tools=(_TOOL,),
        config=Config(max_tokens=123),
    )
    assert registry.plan(key, first, first.system or "").reason == "first_call"
    registry.opened(key, "conversation-1", first, first.system or "")

    lm = SummaryLM(lambda seen: "CHECKPOINT")
    loop_summarizer(lambda: lm, _base)("write the checkpoint")
    [sent] = lm.requests
    plan = registry.plan(key, sent, sent.system or "")
    assert (plan.handle, plan.reason) == ("conversation-1", None)
    texts = [_text(m) for m in plan.messages]
    assert texts == ["HITS-1", "write the checkpoint"]


def _text(message: Message) -> str:
    return "".join(getattr(part, "text", "") for part in message.parts)
