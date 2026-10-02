"""B17: typed plan/usage-limit classification, from STRUCTURED SDK fields only.

Never a substring search over ``ResultMessage.result`` prose -- see the SDK
evidence in ``claude_code_plan_limit``'s module docstring
(``RateLimitInfo.status``, ``ResultMessage.api_error_status``).
"""

from __future__ import annotations

from types import SimpleNamespace

import dspy

from clio_agent.providers.claude_code_plan_limit import (
    PLAN_LIMIT_HTTP_STATUS,
    ClaudeCodePlanLimitError,
    claude_code_plan_limit_message,
    contains_claude_code_plan_limit,
    plan_limit_from_rate_limit_event,
    plan_limit_from_result,
)


def _rate_limit_event(status: str, **info_kwargs: object) -> SimpleNamespace:
    info = SimpleNamespace(status=status, **info_kwargs)
    return SimpleNamespace(rate_limit_info=info)


def _result_message(
    *, is_error: bool, api_error_status: object, result: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(is_error=is_error, api_error_status=api_error_status, result=result)


def test_rate_limit_event_rejected_becomes_a_typed_plan_limit_error() -> None:
    event = _rate_limit_event("rejected", rate_limit_type="five_hour", resets_at=1234)
    err = plan_limit_from_rate_limit_event(event)
    assert isinstance(err, ClaudeCodePlanLimitError)
    assert err.rate_limit_type == "five_hour"
    assert err.resets_at == 1234
    assert "usage limit" in str(err).lower()


def test_rate_limit_event_allowed_is_not_a_limit() -> None:
    assert plan_limit_from_rate_limit_event(_rate_limit_event("allowed")) is None


def test_rate_limit_event_allowed_warning_is_not_yet_a_hard_limit() -> None:
    """SABOTAGE: treat the warning tier as a hard limit -> a session gets cut
    off before it actually hit the wall -> red."""
    assert plan_limit_from_rate_limit_event(_rate_limit_event("allowed_warning")) is None


def test_result_429_becomes_a_typed_plan_limit_error() -> None:
    result = _result_message(
        is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result="quota exhausted"
    )
    err = plan_limit_from_result(result)
    assert isinstance(err, ClaudeCodePlanLimitError)
    assert "429" in str(err)
    assert "quota exhausted" in str(err)


def test_result_non_429_error_is_not_a_plan_limit() -> None:
    """SABOTAGE: classify any is_error result as a plan limit -> a genuine 500
    server error gets mislabeled as a usage-limit hit -> red."""
    result = _result_message(is_error=True, api_error_status=500, result="server error")
    assert plan_limit_from_result(result) is None


def test_result_success_is_not_a_plan_limit() -> None:
    result = _result_message(is_error=False, api_error_status=None)
    assert plan_limit_from_result(result) is None


def test_plan_limit_error_message_is_never_classified_transient() -> None:
    """DSPy must never retry it: a plan-limit hit is terminal for this window."""
    from dspy.utils.exceptions import is_retryable_lm_error

    err = plan_limit_from_result(
        _result_message(is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result="")
    )
    assert err is not None
    assert not is_retryable_lm_error(err)


def test_rate_limit_event_message_names_the_model() -> None:
    """#1529: a user who has since switched providers must be able to tell
    which model actually hit the limit."""
    event = _rate_limit_event("rejected", rate_limit_type="five_hour", resets_at=1234)

    err = plan_limit_from_rate_limit_event(event, model="claude-sonnet-5")

    assert err is not None
    assert err.model == "claude-sonnet-5"
    assert "claude-sonnet-5" in str(err)


def _litellm_wrapped_plan_limit(model: str = "claude-sonnet-5") -> tuple[Exception, str]:
    """Reproduce the REAL shape a plan-limit hit takes by the time it reaches
    ``dspy``/CLIO's error presentation layer (#1529 live evidence).

    ``ClaudeCodePlanLimitError`` is raised from inside CLIO's own async
    generator (``claude_code_litellm._astream_sdk``) that LiteLLM's
    ``CustomStreamWrapper`` iterates. LiteLLM does not recognize the exception
    type, so its generic exception-mapping fallback re-wraps it as
    ``litellm.APIConnectionError`` with the ORIGINAL exception's ``str()`` and a
    full ``traceback.format_exc()`` baked into the message (LiteLLM's own
    documented behavior for an unmapped exception type, BerriAI/litellm#4201),
    then wraps THAT in ``litellm.MidStreamFallbackError`` -- which subclasses
    ``ServiceUnavailableError``. ``dspy.LM`` maps the result to
    ``LMTransportError`` (the "connection" substring in the class name).
    """
    clean = plan_limit_from_result(
        _result_message(is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result=""),
        model=model,
    )
    assert clean is not None
    mangled = (
        f"litellm.APIConnectionError: {clean}\n"
        "Traceback (most recent call last):\n"
        '  File ".../litellm/litellm_core_utils/streaming_handler.py", line 2137, in __anext__\n'
        "    processed_chunk = self.chunk_creator(chunk=chunk)\n"
        '  File ".../clio_agent/providers/claude_code_litellm.py", line 795, in astreaming\n'
        "    async for chunk in _astream_sdk(\n"
        '  File ".../clio_agent/providers/claude_code_litellm.py", line 453, in _astream_sdk\n'
        "    async for chunk in _process():\n"
        '  File ".../clio_agent/providers/claude_code_litellm.py", line 448, in _process\n'
        "    raise plan_limit\n"
        f"clio_agent.providers.claude_code_plan_limit.ClaudeCodePlanLimitError: {clean}\n"
    )
    from litellm.exceptions import MidStreamFallbackError

    raw = MidStreamFallbackError(
        message=mangled, model=f"claude_code/{model}", llm_provider="claude_code"
    )
    wrapped = dspy.LM("openrouter/openrouter/free", api_key="test")._wrap_litellm_exception(raw)
    wrapped.__cause__ = raw
    return ExceptionGroup("unhandled errors in a TaskGroup", [wrapped]), str(clean)


def test_litellm_wrapped_plan_limit_still_carries_the_marker() -> None:
    group, _clean = _litellm_wrapped_plan_limit()

    assert contains_claude_code_plan_limit(group) is True


def test_claude_code_plan_limit_message_recovers_the_clean_sentence() -> None:
    """The user-facing text must be CLIO's own sentence, never LiteLLM's
    traceback-laden re-wrap (the live #1529 "Response unavailable" card)."""
    group, clean = _litellm_wrapped_plan_limit(model="claude-sonnet-5")

    message = claude_code_plan_limit_message(group)

    assert message == clean
    assert "claude-sonnet-5" in message
    assert "Traceback" not in message
    assert "ExceptionGroup" not in message
    assert "MidStreamFallbackError" not in message


def test_an_engine_raised_plan_limit_is_never_retried_by_dspy() -> None:
    """#1529 on the DSPy 3.4 engine: the engine raises the typed error bare; DSPy's
    managed-call boundary maps an unclassified engine failure to ``LMUnexpectedError``,
    which it never retries (message text cannot make it retryable), and the plan-limit
    marker is still found on the wrapped error."""
    from dspy.clients.errors import error_boundary
    from dspy.utils.exceptions import is_retryable_lm_error

    err = plan_limit_from_result(
        _result_message(is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result=""),
        model="claude-sonnet-5",
    )
    assert err is not None
    try:
        with error_boundary("claude_code/claude-sonnet-5", provider="claude_code", unexpected=True):
            raise err
    except Exception as wrapped:  # noqa: BLE001 - the boundary's mapped error
        assert not is_retryable_lm_error(wrapped)
        assert contains_claude_code_plan_limit(wrapped) is True
        assert claude_code_plan_limit_message(wrapped) == str(err)
    else:
        raise AssertionError("the boundary did not re-raise")
