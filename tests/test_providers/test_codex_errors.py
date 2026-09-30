"""Unit tests for Codex error classification and retry/backoff (A.7, A.9)."""

from __future__ import annotations

import pytest

from clio_agent.providers.codex.errors import (
    CODEX_PLAN_LIMIT_MESSAGE_MARKER,
    CodexPlanLimitError,
    CodexResponseError,
    codex_plan_limit_message,
    contains_codex_plan_limit,
    is_retryable_status,
    is_usage_limit_text,
    next_retry_delay_ms,
    parse_retry_after_ms,
    raise_for_backend_error,
)


@pytest.mark.parametrize(
    "text",
    [
        "You have hit your usage limit for this plan.",
        "Quota exceeded for this account.",
        "insufficient_quota",
        "Please check your billing details.",
    ],
)
def test_is_usage_limit_text_detects_plan_limit_wording(text: str) -> None:
    assert is_usage_limit_text(text) is True


def test_is_usage_limit_text_false_for_generic_rate_limit() -> None:
    assert is_usage_limit_text("too many requests, please slow down") is False


def test_429_with_usage_limit_wording_is_not_retryable() -> None:
    assert is_retryable_status(429, "usage limit reached") is False


def test_429_without_usage_limit_wording_is_retryable() -> None:
    assert is_retryable_status(429, "rate limited, try again") is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_statuses_are_retryable(status: int) -> None:
    assert is_retryable_status(status) is True


def test_4xx_other_than_429_is_not_retryable() -> None:
    assert is_retryable_status(400) is False
    assert is_retryable_status(404) is False


def test_raise_for_backend_error_terminal_plan_limit() -> None:
    with pytest.raises(CodexPlanLimitError):
        raise_for_backend_error(
            code="rate_limit_exceeded", message="usage limit reached", status_code=429
        )


def test_raise_for_backend_error_generic_response_error() -> None:
    with pytest.raises(CodexResponseError) as exc_info:
        raise_for_backend_error(code="server_error", message="something broke")
    assert not isinstance(exc_info.value, CodexPlanLimitError)


def test_plan_limit_message_keeps_the_backends_own_words() -> None:
    """#1529 Codex analog: the marker is CLIO's own, but the backend's words
    (which vary by account/plan) must still reach the user."""
    err = CodexPlanLimitError("you have exceeded your monthly usage limit", status_code=429)

    assert str(err).startswith(CODEX_PLAN_LIMIT_MESSAGE_MARKER)
    assert "monthly usage limit" in str(err)
    assert err.detail == "you have exceeded your monthly usage limit"


def test_litellm_wrapped_codex_plan_limit_still_recovers_the_clean_sentence() -> None:
    """#1529: the Codex ``astreaming`` re-raises its typed error bare (like
    Claude Code's), so it hits the SAME LiteLLM generic exception-mapping
    fallback (BerriAI/litellm#4201) that bakes a full traceback into the
    wrapper's own message."""
    clean = str(CodexPlanLimitError("usage limit reached", status_code=429))
    mangled = RuntimeError(
        f"litellm.MidStreamFallbackError: litellm.APIConnectionError: {clean}\n"
        "Traceback (most recent call last):\n"
        '  File ".../codex/litellm_adapter.py", line 405, in astreaming\n'
        "    raise\n"
        f"clio_agent.providers.codex.errors.CodexPlanLimitError: {clean}\n"
    )
    group = ExceptionGroup("unhandled errors in a TaskGroup", [mangled])

    assert contains_codex_plan_limit(group) is True
    message = codex_plan_limit_message(group)
    assert message == clean
    assert "Traceback" not in message


def test_litellm_wrapped_codex_plan_limit_is_never_classified_transient() -> None:
    """SABOTAGE (regression, #1529): same misclassification risk as Claude
    Code's -- MidStreamFallbackError/APIConnectionError are transient markers
    on their own."""
    from clio_agent.lm.io_logging import _is_transient_provider_error

    clean = str(CodexPlanLimitError("usage limit reached", status_code=429))
    mangled = RuntimeError(
        f"litellm.MidStreamFallbackError: litellm.APIConnectionError: {clean}\n"
        "Traceback (most recent call last):\n"
        "    raise\n"
        f"clio_agent.providers.codex.errors.CodexPlanLimitError: {clean}\n"
    )

    assert _is_transient_provider_error(mangled) is False


class TestRetryAfterParsing:
    def test_retry_after_ms_wins_over_retry_after(self) -> None:
        headers = {"retry-after-ms": "1500", "retry-after": "5"}
        assert parse_retry_after_ms(headers) == 1500.0

    def test_retry_after_seconds_converted_to_ms(self) -> None:
        assert parse_retry_after_ms({"retry-after": "2"}) == 2000.0

    def test_no_header_returns_none(self) -> None:
        assert parse_retry_after_ms({}) is None

    def test_case_insensitive_header_lookup(self) -> None:
        assert parse_retry_after_ms({"Retry-After-Ms": "300"}) == 300.0


class TestNextRetryDelay:
    def test_honors_server_retry_after(self) -> None:
        decision = next_retry_delay_ms(
            attempt=0, headers={"retry-after-ms": "2000"}, max_delay_ms=60_000
        )
        assert decision.should_retry is True
        assert decision.delay_ms == 2000.0

    def test_server_delay_exceeding_cap_refuses_retry(self) -> None:
        decision = next_retry_delay_ms(
            attempt=0, headers={"retry-after-ms": "120000"}, max_delay_ms=60_000
        )
        assert decision.should_retry is False
        assert decision.exceeded_cap is True

    def test_exponential_backoff_without_server_hint(self) -> None:
        first = next_retry_delay_ms(attempt=0, base_delay_ms=1000, max_delay_ms=60_000)
        second = next_retry_delay_ms(attempt=1, base_delay_ms=1000, max_delay_ms=60_000)
        third = next_retry_delay_ms(attempt=2, base_delay_ms=1000, max_delay_ms=60_000)
        assert (first.delay_ms, second.delay_ms, third.delay_ms) == (1000.0, 2000.0, 4000.0)

    def test_exponential_backoff_capped(self) -> None:
        decision = next_retry_delay_ms(attempt=10, base_delay_ms=1000, max_delay_ms=60_000)
        assert decision.delay_ms == 60_000.0
        assert decision.should_retry is True
