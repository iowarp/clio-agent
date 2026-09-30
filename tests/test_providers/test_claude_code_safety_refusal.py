"""#1529 follow-up: a Claude Code safety-filter refusal must be typed, never
retried, and never dumped as a raw traceback -- the same failure mode the
plan-limit fix (#1529) closed, generalized via
:mod:`clio_agent.providers.terminal_signal`.

The detection text below is the CLI's own real result (a live GACT session
export, ``release-usgs-claude_code-sess_6524486972b3.json``): Anthropic's
canned safety-refusal template, with no dedicated ``api_error_status`` the
way the 401/404/429 signals carry one.
"""

from __future__ import annotations

from types import SimpleNamespace

import dspy

from clio_agent.providers.claude_code_result_errors import raise_classified_result_error
from clio_agent.providers.claude_code_safety_refusal import (
    SAFETY_REFUSAL_MARKER,
    ClaudeCodeSafetyRefusalError,
    is_safety_refusal_text,
    safety_refusal_from_result,
)
from clio_agent.providers.terminal_signal import (
    find_terminal_signal,
    is_terminal_provider_error,
    recover_message,
)
from clio_agent.providers.terminal_signal_catalog import TERMINAL_PROVIDER_SIGNALS

#: Verbatim from the live session export (redacted ids are still real-shaped).
_LIVE_RESULT_TEXT = (
    "API Error: Sonnet 5.5's safeguards flagged this message "
    "(https://www.anthropic.com/legal/aup). This sometimes happens with safe, "
    "normal conversations. Claude Code can't respond to this message with "
    "Sonnet 5.5. Try rephrasing the request in a new session or change your "
    "model. Learn more: https://support.claude.com/en/articles/8106465 "
    "Details: `[reasoning_extraction]` "
    "Request ID: req_011CfZQPaqG8j2jMNdHze8aM "
    "Message ID: msg_011CfZQPihXnP85Dt6PBtwoY"
)


def _result_message(*, is_error: bool, result: str = "") -> SimpleNamespace:
    return SimpleNamespace(is_error=is_error, api_error_status=None, result=result)


def test_is_safety_refusal_text_detects_the_live_wording() -> None:
    assert is_safety_refusal_text(_LIVE_RESULT_TEXT) is True


def test_is_safety_refusal_text_false_for_an_unrelated_error() -> None:
    assert is_safety_refusal_text("connection reset by peer") is False


def test_safety_refusal_from_result_classifies_the_live_text() -> None:
    err = safety_refusal_from_result(
        _result_message(is_error=True, result=_LIVE_RESULT_TEXT), model="sonnet"
    )

    assert isinstance(err, ClaudeCodeSafetyRefusalError)
    assert err.model == "sonnet"
    assert err.request_id == "req_011CfZQPaqG8j2jMNdHze8aM"
    assert err.message_id == "msg_011CfZQPihXnP85Dt6PBtwoY"
    assert str(err).startswith(SAFETY_REFUSAL_MARKER) or "[cc_request_id=" in str(err)


def test_safety_refusal_from_result_none_for_success() -> None:
    assert safety_refusal_from_result(_result_message(is_error=False)) is None


def test_safety_refusal_from_result_none_for_unrelated_error() -> None:
    assert safety_refusal_from_result(_result_message(is_error=True, result="server error")) is None


def test_message_is_the_short_plain_sentence_the_coordinator_specified() -> None:
    """The exact wording requested for #1529's safety-refusal follow-up."""
    err = safety_refusal_from_result(_result_message(is_error=True, result=_LIVE_RESULT_TEXT))
    assert err is not None

    message = recover_message(err, SAFETY_REFUSAL_MARKER)

    assert message == (
        "Claude declined this request (its safety filter flagged it, which can "
        "happen with normal requests). Rephrase it in a new session or switch the model."
    )
    assert "req_" not in message  # the request id is a detail, not in the sentence
    assert "Request ID" not in message


def test_request_id_is_recovered_even_from_the_recovered_message_alone() -> None:
    """The request id token rides BEFORE the marker so it never leaks into the
    displayed sentence, but the raw exception text still carries it for
    ``extra_details`` to parse after LiteLLM's re-wrap."""
    err = safety_refusal_from_result(_result_message(is_error=True, result=_LIVE_RESULT_TEXT))
    assert err is not None

    assert "[cc_request_id=req_011CfZQPaqG8j2jMNdHze8aM]" in str(err)


def test_raise_classified_result_error_raises_the_typed_safety_refusal() -> None:
    """The central classifier (``claude_code_result_errors.py``) must catch
    this BEFORE the caller's generic ``ClaudeCodeExecError`` fallback -- the
    live evidence shows the raw text reaching the transcript specifically
    because nothing here classified it."""
    import pytest

    with pytest.raises(ClaudeCodeSafetyRefusalError):
        raise_classified_result_error(
            _result_message(is_error=True, result=_LIVE_RESULT_TEXT),
            model="sonnet",
            assistant_error=None,
        )


def test_registered_in_the_terminal_signal_catalog() -> None:
    err = safety_refusal_from_result(_result_message(is_error=True, result=_LIVE_RESULT_TEXT))
    assert err is not None

    assert is_terminal_provider_error(err, TERMINAL_PROVIDER_SIGNALS) is True
    found = find_terminal_signal(err, TERMINAL_PROVIDER_SIGNALS)
    assert found is not None
    signal, node = found
    assert signal.reason == "provider_safety_refusal"
    assert signal.provider_id == "claude_code"
    extra = signal.extra_details(node)
    assert extra["request_id"] == "req_011CfZQPaqG8j2jMNdHze8aM"
    assert extra["message_id"] == "msg_011CfZQPihXnP85Dt6PBtwoY"


def _litellm_wrapped_safety_refusal() -> tuple[Exception, ClaudeCodeSafetyRefusalError]:
    """Reproduce the EXACT live shape (#1529 session export): LiteLLM's
    generic exception-mapping fallback (BerriAI/litellm#4201) bakes a full
    traceback into ``MidStreamFallbackError``/``APIConnectionError`` because
    it does not recognize ``ClaudeCodeSafetyRefusalError``."""
    from litellm.exceptions import MidStreamFallbackError

    clean = safety_refusal_from_result(
        _result_message(is_error=True, result=_LIVE_RESULT_TEXT), model="sonnet"
    )
    assert clean is not None
    mangled = (
        f"litellm.APIConnectionError: claude agent sdk returned an error for "
        f"model=sonnet: {clean}\n"
        "Traceback (most recent call last):\n"
        '  File ".../litellm/litellm_core_utils/streaming_handler.py", line 2137, in __anext__\n'
        "    async for chunk in self.completion_stream:\n"
        '  File ".../clio_agent/providers/claude_code_litellm.py", line 795, in astreaming\n'
        "    async for chunk in _astream_sdk(\n"
        '  File ".../clio_agent/providers/claude_code_litellm.py", line 433, in _process\n'
        "    raise ClaudeCodeExecError(\n"
        f"clio_agent.providers.claude_code_litellm.ClaudeCodeExecError: "
        f"claude agent sdk returned an error for model=sonnet: {clean}\n"
    )
    raw = MidStreamFallbackError(
        message=mangled, model="claude_code/sonnet", llm_provider="claude_code"
    )
    wrapped = dspy.LM("openrouter/openrouter/free", api_key="test")._wrap_litellm_exception(raw)
    wrapped.__cause__ = raw
    return ExceptionGroup("unhandled errors in a TaskGroup", [wrapped]), clean


def test_litellm_wrapped_safety_refusal_recovers_the_clean_sentence() -> None:
    group, clean = _litellm_wrapped_safety_refusal()

    found = find_terminal_signal(group, TERMINAL_PROVIDER_SIGNALS)
    assert found is not None
    signal, node = found
    assert signal.reason == "provider_safety_refusal"
    message = recover_message(node, signal.marker)
    assert message == (
        "Claude declined this request (its safety filter flagged it, which can "
        "happen with normal requests). Rephrase it in a new session or switch the model."
    )
    assert "Traceback" not in message
    assert "ExceptionGroup" not in message
    extra = signal.extra_details(node)
    assert extra["request_id"] == clean.request_id


def test_litellm_wrapped_safety_refusal_is_never_classified_transient() -> None:
    """SABOTAGE (regression, #1529 follow-up): the SAME litellm re-wrap trap
    the plan-limit fix closed -- MidStreamFallbackError/APIConnectionError are
    transient markers on their own, so an unrecognized terminal exception type
    would otherwise be silently retried."""
    from clio_agent.lm.io_logging import _is_transient_provider_error

    group, _clean = _litellm_wrapped_safety_refusal()

    assert _is_transient_provider_error(group.exceptions[0]) is False


def test_agent_forward_error_info_is_typed_with_request_id_in_details() -> None:
    from clio_agent.gact.stream_failures import (
        PROVIDER_SAFETY_REFUSAL_REASON,
        agent_forward_error_info,
    )

    state = SimpleNamespace(
        user_msg=SimpleNamespace(
            metadata={"effective_model": {"provider_id": "claude_code", "model_id": "sonnet"}}
        )
    )
    group, clean = _litellm_wrapped_safety_refusal()

    info = agent_forward_error_info(state, group)

    assert info.error == "provider_error"
    assert info.message == (
        "Claude declined this request (its safety filter flagged it, which can "
        "happen with normal requests). Rephrase it in a new session or switch the model."
    )
    assert "Traceback" not in info.message
    assert info.details["reason"] == PROVIDER_SAFETY_REFUSAL_REASON
    assert info.details["request_id"] == clean.request_id
    assert info.recoverable is True
