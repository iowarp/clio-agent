"""A reply missing only the header of a signature's one text field still parses.

Live (deep-research writer): the model wrote the whole Markdown report without the
``[[ ## report ## ]]`` header, the parse failed and the run lost its report after 25
minutes of research. The header is protocol framing; every other parse is unchanged.
"""

from __future__ import annotations

import logging

import dspy
import pytest
from dspy.utils.exceptions import AdapterParseError

from clio_agent.lm.adapters import _chat_adapter_cls

REPORT = "# Title\n\n## Findings\n\n- One [source](https://example.org).\n"


class WriteReport(dspy.Signature):
    """Write the report."""

    question: str = dspy.InputField()
    report: str = dspy.OutputField()


class Judge(dspy.Signature):
    """Judge two answers."""

    question: str = dspy.InputField()
    winner: str = dspy.OutputField()
    reason: str = dspy.OutputField()


class Count(dspy.Signature):
    """Count."""

    question: str = dspy.InputField()
    total: int = dspy.OutputField()


def _adapter() -> dspy.ChatAdapter:
    return _chat_adapter_cls()(use_json_adapter_fallback=False)


def test_an_unmarked_reply_is_the_one_text_field(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="clio_agent.lm.adapters"):
        parsed = _adapter().parse(WriteReport, REPORT)

    assert parsed == {"report": REPORT.strip()}
    assert "reason=adapter_unmarked_text_field field=report" in caplog.text


def test_a_marked_reply_parses_as_before(caplog: pytest.LogCaptureFixture) -> None:
    reply = f"[[ ## report ## ]]\n{REPORT}\n[[ ## completed ## ]]"
    with caplog.at_level(logging.WARNING, logger="clio_agent.lm.adapters"):
        assert _adapter().parse(WriteReport, reply) == {"report": REPORT.strip()}
    assert "adapter_unmarked_text_field" not in caplog.text


@pytest.mark.parametrize("signature", [Judge, Count])
def test_any_other_signature_still_fails_typed(signature: type[dspy.Signature]) -> None:
    with pytest.raises(AdapterParseError):
        _adapter().parse(signature, "Answer A is better.")


def test_an_empty_reply_still_fails_typed() -> None:
    with pytest.raises(AdapterParseError):
        _adapter().parse(WriteReport, "   ")
