"""A request larger than the model's window is a typed context-length error."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from dspy.lm15 import ContextLengthError

from clio_agent.providers.claude_code_result_errors import raise_classified_result_error


def test_prompt_is_too_long_raises_context_length_error() -> None:
    msg = SimpleNamespace(is_error=True, result="Prompt is too long", api_error_status=None)
    with pytest.raises(ContextLengthError, match="Prompt is too long"):
        raise_classified_result_error(msg, model="claude-opus-5-5", assistant_error=None)


def test_an_unrelated_error_is_left_to_the_caller() -> None:
    msg = SimpleNamespace(is_error=True, result="overloaded", api_error_status=None)
    assert raise_classified_result_error(msg, model="m", assistant_error=None) is None
