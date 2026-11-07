"""Test doubles for the compaction summarizer (``gact.compaction_summarizer``).

The summarizer is a model request: the agent's own context plus the compaction prompt.
:class:`SummaryLM` stands in for that model. It answers with ``reply(seen)``, where
``seen`` is the whole request as text, so a test can check that the summarizer saw
the history it replaces and the prompt it was given.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dspy.lm15 import Message, Request, Response, TextPart, Usage

from clio_agent.gact.compaction import AutoCompactionGuard

#: Separates the history from the compaction prompt in :func:`request_text`.
PROMPT_MARK = "\n--- compaction prompt ---\n"


def request_text(request: Request) -> str:
    """The request as text: the history one line per part (the transcript form
    compaction renders), then :data:`PROMPT_MARK` and the last message, the prompt."""
    from clio_agent.gact.compaction import _message_lines  # noqa: PLC0415

    *history, prompt = request.messages
    lines = "\n".join(line for m in history for line in _message_lines(m))
    text = "".join(getattr(part, "text", "") for part in prompt.parts)
    return f"{lines}{PROMPT_MARK}{text}"


def split_seen(seen: str) -> tuple[str, str]:
    """``(history, prompt)`` of a :func:`request_text`."""
    history, _, prompt = seen.partition(PROMPT_MARK)
    return history, prompt


class SummaryLM:
    """A model that answers each request with ``reply(request_text(request))``."""

    model = "test/summary"

    def __init__(self, reply: Callable[[str], str]) -> None:
        self.reply = reply
        self.requests: list[Request] = []

    async def acall(self, request: Request) -> Response:
        self.requests.append(request)
        text = self.reply(request_text(request))
        return Response(
            id=None,
            model=self.model,
            message=Message(role="assistant", parts=(TextPart(text=text),)),
            finish_reason="stop",
            usage=Usage(),
        )


def summary_lm(reply: Callable[[str], str]) -> SummaryLM:
    """A :class:`SummaryLM` answering with ``reply``."""
    return SummaryLM(reply)


def auto_guard() -> AutoCompactionGuard:
    """A guard whose summarizer reads the active scope's context on the active app's
    agent model, as the loop's own request would (for tests that drive
    ``maybe_autocompact`` without a loop)."""
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415
    from clio_agent.gact.compaction_summarizer import scope_summarizer  # noqa: PLC0415

    def summarize(prompt: str) -> str:
        app: Any = ctx.active_app()
        _arc, session, scope = arc_scope()
        return scope_summarizer(app, session, scope, app.state.agent._main_lm)(prompt)

    return AutoCompactionGuard(summarize=summarize)


def keep_open_request_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep only the open turn's request verbatim (no typed-message budget, no step).

    For tests that pin the record and fold mechanics on small seeded contexts made of
    typed messages, which the default keep rule would keep whole (nothing to fold).
    The default rule has its own tests (``test_compaction_keep_policy.py``).
    """
    monkeypatch.setenv("CLIO_COMPACTION_KEEP_USER_CHARS", "0")
    monkeypatch.setenv("CLIO_COMPACTION_KEEP_LAST_STEPS", "0")
