"""An answer or prompt the canonical tokenizer cannot line up with the capture.

No heat can be shown then; the failure is logged as a warning and its detail names the
likely cause instead of a bare count mismatch.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import numpy as np
import pytest

from clio_agent.gact.attention.alignment import (
    CAPTURE_MISMATCH,
    NON_CANONICAL,
    UNKNOWN,
    diagnosed,
)
from clio_agent.gact.attention.capture import _check_prompt
from clio_agent.gact.attention.chat_render import Encoded
from clio_agent.gact.attention.lm_calls import LmCall, _from_payload
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.selection import output_steps

_TOKEN = re.compile(r".", re.S)


class _CharRenderer:
    """One token per char, as a named tokenizer."""

    identity = "char-tokenizer"

    def encode(self, text: str) -> Encoded:
        spans = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
        return Encoded(text=text, ids=[ord(text[a]) for a, _ in spans], offsets=spans)


def _record(steps: int) -> SimpleNamespace:
    return SimpleNamespace(clean=True, decode_steps=steps, request_id="chatcmpl-x", health={})


def _call(**extra: object) -> LmCall:
    return LmCall(
        event_id="evt-1",
        session_id="sess-1",
        turn_id="turn-1",
        occurred_at="",
        model="m",
        response_id="chatcmpl-x",
        messages=[],
        content="ANSWER: Ecm14",
        declaration=None,
        source="test",
        extra=dict(extra),
    )


def _mismatch(call: LmCall, steps: int) -> AttentionUnavailable:
    """The answer (13 chars, so 13 canonical tokens) against a capture of ``steps``."""
    with pytest.raises(AttentionUnavailable) as err, diagnosed(call):
        output_steps(_CharRenderer(), call.content or "", 8, 13, _record(steps))
    return err.value


def test_sampled_non_canonical_tokens_are_named_as_the_cause() -> None:
    # The provider counted 15 generated tokens, the capture has 16 steps (+ stop):
    # the model's tokens are not the canonical split of its own text.
    err = _mismatch(_call(output_tokens=15, finish_reason="stop"), 16)

    assert err.reason == "output_alignment_mismatch"
    assert err.context["likely_cause"] == NON_CANONICAL
    assert (err.context["output_tokens"], err.context["decode_steps"]) == (13, 16)
    assert err.context["provider_output_tokens"] == 15
    assert err.context["tokenizer"] == "char-tokenizer"
    assert "temperature 0" in err.detail and "char-tokenizer" in err.detail


def test_a_capture_that_is_not_this_answer_is_named_as_the_cause() -> None:
    err = _mismatch(_call(output_tokens=13, finish_reason="stop"), 30)
    assert err.context["likely_cause"] == CAPTURE_MISMATCH
    assert "does not belong to this answer" in err.detail


def test_without_a_provider_count_the_usual_causes_are_listed() -> None:
    err = _mismatch(_call(), 30)
    assert err.context["likely_cause"] == UNKNOWN
    assert "temperature > 0" in err.detail


def test_an_alignment_failure_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.attention.alignment"):
        _mismatch(_call(output_tokens=15), 16)
    assert any(
        r.levelno == logging.WARNING and "output_alignment_mismatch" in r.getMessage()
        for r in caplog.records
    )


def test_other_reasons_pass_through_untouched(caplog: pytest.LogCaptureFixture) -> None:
    original = AttentionUnavailable("selection_not_located", "nothing selected")
    with caplog.at_level(logging.WARNING), pytest.raises(AttentionUnavailable) as err:
        with diagnosed(_call()):
            raise original
    assert err.value is original
    assert not caplog.records


def test_a_prompt_mismatch_names_the_first_differing_token_and_the_tokenizer() -> None:
    encoded = Encoded(text="abcd", ids=[1, 2, 9, 4], offsets=[(0, 1), (1, 2), (2, 3), (3, 4)])
    summary = SimpleNamespace(prompt_tokens=4, prompt_token_ids=np.array([1, 2, 3, 4]))
    with pytest.raises(AttentionUnavailable) as err:
        _check_prompt(encoded, summary, "tok-a (recorded: tok-b)")
    assert err.value.reason == "range_alignment_mismatch"
    assert err.value.context["first_difference"] == 2
    assert err.value.context["tokenizer"] == "tok-a (recorded: tok-b)"
    assert "first difference at token 2" in err.value.detail


def test_a_matching_prompt_passes() -> None:
    encoded = Encoded(text="ab", ids=[1, 2], offsets=[(0, 1), (1, 2)])
    _check_prompt(encoded, SimpleNamespace(prompt_tokens=2, prompt_token_ids=np.array([1, 2])))


def test_the_provider_counts_ride_on_the_call() -> None:
    call = _from_payload(
        event_id="e",
        session_id="s",
        turn_id="t",
        occurred_at="",
        payload={
            "content": "x",
            "messages": [],
            "finish_reason": "stop",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
        metadata={},
        source="test",
    )
    assert call.extra == {"finish_reason": "stop", "output_tokens": 3}
