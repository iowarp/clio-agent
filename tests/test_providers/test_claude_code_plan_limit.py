"""B17: typed plan/usage-limit classification, from STRUCTURED SDK fields only.

Never a substring search over ``ResultMessage.result`` prose -- see the SDK
evidence in ``claude_code_plan_limit``'s module docstring
(``RateLimitInfo.status``, ``ResultMessage.api_error_status``).
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.providers.claude_code_plan_limit import (
    PLAN_LIMIT_HTTP_STATUS,
    ClaudeCodePlanLimitError,
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
    """The error must never contain one of lm.io_logging's transient markers --
    a plan-limit hit is terminal for this window, not worth an immediate retry."""
    from clio_agent.lm.io_logging import _is_transient_provider_error

    err = plan_limit_from_result(
        _result_message(is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result="")
    )
    assert err is not None
    assert _is_transient_provider_error(err) is False
