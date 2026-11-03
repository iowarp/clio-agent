"""The compaction summarizer: the agent's own context, plus the checkpoint request.

The checkpoint is written by the model over exactly what the agent sees: its system
text, its tools and its messages (the request, every message the person typed, the
plan, the todos, an earlier checkpoint at the head), followed by one user message, the
rendered compaction prompt (``clio_agent.compaction_prompt``).

Inside a loop (the automatic trigger) that request is the agent's next request with the
compaction prompt appended, sent on the loop's own model: a provider that keeps the
conversation (``claude_code``, Codex) is sent only the new messages, and any prefix
cache (Anthropic, OpenAI, vLLM, LM Studio) serves the rest. The tools stay declared and
the config unchanged, because changing either invalidates a provider's cached prefix;
the prompt says the reply is text and runs no tools. Before this, the summary ran
through the host's legacy chat module with the history pasted into one question: no
cache (0 and 789 cached tokens on the H5 compactions, against 33k to 158k on the agent
steps around them) and no sight of the request it was summarizing
(``docs/design/compaction-review-2026-11.md``).

Outside a loop (the manual trigger between turns) no conversation is kept to continue,
so the scope's context is read once and sent with the prompt on the session's model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dspy.lm15 import Message, Request, Response, TextPart

__all__ = ["Summarize", "loop_summarizer", "reply_text", "scope_summarizer"]

#: Writes the checkpoint for one rendered compaction prompt.
Summarize = Callable[[str], str]


def reply_text(response: Response) -> str:
    """The visible text of the summarizer's reply (thinking and tool calls left out)."""
    from clio_agent.gact.agents.clio_react_stream import reply_thinking_and_text  # noqa: PLC0415

    _thinking, text, _origin = reply_thinking_and_text(response)
    return text


def _ask(lm: Any, request: Request) -> str:
    from clio_agent.lm.engines.lm_loop import run_on_lm_loop  # noqa: PLC0415

    return reply_text(run_on_lm_loop(lambda: lm.acall(request)))


def _with_prompt(messages: Any, prompt: str) -> tuple[Message, ...]:
    return (*messages, Message(role="user", parts=(TextPart(text=prompt),)))


def loop_summarizer(lm: Callable[[], Any], next_request: Callable[[], Request]) -> Summarize:
    """The summarizer of a running loop: its next request, plus the compaction prompt.

    Args:
        lm: The loop's current model (its provider conversation is continued).
        next_request: Builds the request the loop would send now, without committing
            input or recording anything.
    """

    def summarize(prompt: str) -> str:
        base = next_request()
        return _ask(
            lm(),
            Request(
                model=base.model,
                system=base.system,
                messages=_with_prompt(base.messages, prompt),
                tools=base.tools,
                config=base.config,
            ),
        )

    return summarize


def scope_summarizer(app: Any, sid: str, scope: str, lm: Any) -> Summarize:
    """The summarizer outside a loop: ``scope``'s context read once, on ``lm``."""
    from clio_agent.gact.agents.clio_react_record import fold_steps  # noqa: PLC0415
    from clio_agent.gact.agents.context_reader import model_context_segments  # noqa: PLC0415
    from clio_agent.lm.hooked_lm import wrap_lm_with_hooks  # noqa: PLC0415 - model hooks

    def summarize(prompt: str) -> str:
        # The loop's own read, with the app's content resolver for confirmed input.
        segments = app.state.arc.context_view(sid, scope).segments
        history = getattr(app.state, "task_history", None)
        messages = fold_steps(model_context_segments(segments, history, sid, scope))
        model = wrap_lm_with_hooks(lm)
        return _ask(model, Request(model=model.model, messages=_with_prompt(messages, prompt)))

    return summarize
