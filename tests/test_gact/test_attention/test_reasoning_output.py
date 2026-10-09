"""Output alignment for a reasoning-parser call (F050).

vLLM's reasoning parser splits ``<think>reasoning</think>content`` into two
fields; the capture has a decode step for every generated token, so the
content's steps start after the reasoning prefix.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from clio_agent.gact.attention.chat_render import Encoded
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.selection import output_steps

_TOKEN = re.compile(r"<think>|</think>|.", re.S)


class _CharRenderer:
    """One token per char; the think delimiters are single tokens."""

    def encode(self, text: str) -> Encoded:
        spans = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
        return Encoded(text=text, ids=[hash(text[a:b]) for a, b in spans], offsets=spans)


def _record(steps: int) -> SimpleNamespace:
    return SimpleNamespace(clean=True, decode_steps=steps, request_id="chatcmpl-x", health={})


def test_content_steps_are_offset_by_the_reasoning_prefix() -> None:
    reasoning, content = "\nhmm\n", "\n\nAnswer."
    prefix = 1 + len(reasoning) + 1
    total = prefix + len(content) + 1  # + stop token
    span = output_steps(_CharRenderer(), content, 2, 8, _record(total), reasoning=reasoning)
    assert span.steps == list(range(prefix + 2, prefix + 8))
    assert (span.token_lo, span.token_hi, span.output_tokens) == (2, 8, len(content))


def test_without_recorded_reasoning_the_mismatch_stays_typed() -> None:
    content = "\n\nAnswer."
    with pytest.raises(AttentionUnavailable) as err:
        output_steps(_CharRenderer(), content, 0, 3, _record(40))
    assert err.value.reason == "output_alignment_mismatch"
    assert "reasoning not recorded" in err.value.detail


def test_reasoning_that_does_not_explain_the_count_is_not_a_shifted_guess() -> None:
    with pytest.raises(AttentionUnavailable) as err:
        output_steps(_CharRenderer(), "abc", 0, 2, _record(50), reasoning="short")
    assert err.value.reason == "output_alignment_mismatch"


def test_content_only_capture_ignores_reasoning() -> None:
    span = output_steps(_CharRenderer(), "abc", 0, 2, _record(4), reasoning="r")
    assert span.steps == [0, 1]
