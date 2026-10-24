"""Unit tests for Codex error classification and retry/backoff (A.7, A.9)."""

from __future__ import annotations

import pytest

from clio_agent.providers.codex.errors import (
    CODEX_PLAN_LIMIT_MESSAGE_MARKER,
    CodexPlanLimitError,
    codex_plan_limit_message,
    contains_codex_plan_limit,
    is_usage_limit_body,
    is_usage_limit_error,
    next_retry_delay_ms,
    parse_retry_after_ms,
    usage_limit_resets_at,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "error", "error": {"type": "usage_limit_reached", "message": "any words"}},
        {"type": "error", "error": {"code": "insufficient_quota"}},
        {"type": "response.failed", "response": {"error": {"code": "insufficient_quota"}}},
    ],
)
def test_usage_limit_is_read_from_the_error_code_or_type(payload: dict) -> None:
    assert is_usage_limit_error(payload) is True


def test_usage_limit_wording_alone_is_not_a_usage_limit() -> None:
    """The backend's prose is never interpreted: only its code or type counts."""
    payload = {"type": "error", "error": {"code": "rate_limit_exceeded", "message": "usage limit"}}
    assert is_usage_limit_error(payload) is False
    assert is_usage_limit_body('{"error": {"message": "You have hit your usage limit"}}') is False


def test_usage_limit_body_parses_json_and_rejects_non_json() -> None:
    assert is_usage_limit_body('{"error": {"type": "usage_limit_reached"}}') is True
    assert is_usage_limit_body("usage limit reached") is False


def test_usage_limit_reset_time_from_either_field() -> None:
    assert usage_limit_resets_at({"error": {"resets_at": 1_800_000_000}}) == 1_800_000_000
    relative = usage_limit_resets_at({"error": {"resets_in_seconds": 60}})
    assert relative is not None and relative > 1_700_000_000
    assert usage_limit_resets_at({"error": {}}) is None


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


def test_a_codex_plan_limit_is_never_retried_by_dspy() -> None:
    """SABOTAGE (regression, #1529): a plan-limit hit is terminal. DSPy 3.4 owns
    retries and re-issues only its own retryable error types; the typed error is not
    one, whatever its text says."""
    from dspy.utils.exceptions import is_retryable_lm_error

    assert not is_retryable_lm_error(CodexPlanLimitError("usage limit reached", status_code=429))


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
