"""Provider HTTP errors reach the user as one plain line (rel18 live finding)."""

from __future__ import annotations

import dspy
import litellm
import pytest

from clio_agent.gact.stream_failures import (
    describe_stream_exc,
    failed_before_output,
    provider_failure_message,
)

_OPENROUTER_BODY = '{"error":{"message":"No endpoints available for openrouter/free","code":404}}'


def _wrapped(raw: Exception) -> Exception:
    """Wrap ``raw`` exactly the way ``dspy.LM`` does at the LiteLLM boundary."""

    wrapped = dspy.LM("openrouter/openrouter/free", api_key="test")._wrap_litellm_exception(raw)
    wrapped.__cause__ = raw
    return wrapped


def test_provider_error_is_one_plain_line_in_the_providers_own_words() -> None:
    raw = litellm.NotFoundError(
        message=f"OpenrouterException - {_OPENROUTER_BODY}",
        model="openrouter/free",
        llm_provider="openrouter",
    )
    exc = ExceptionGroup("unhandled errors in a TaskGroup", [_wrapped(raw)])

    message = provider_failure_message(exc, provider_label="OpenRouter")

    assert message == "OpenRouter: No endpoints available for openrouter/free (HTTP 404)"
    # The trace keeps the full, unwrapped detail.
    assert "LMUnsupportedModelError" in describe_stream_exc(exc, provider_id="openrouter")


def test_provider_error_without_a_json_body_keeps_the_provider_text() -> None:
    raw = litellm.RateLimitError(
        message="OpenrouterException - rate limited upstream\nretry later",
        model="openrouter/free",
        llm_provider="openrouter",
    )

    message = provider_failure_message(_wrapped(raw), provider_label="OpenRouter")

    assert message is not None
    assert message.startswith("OpenRouter: ")
    assert "rate limited upstream retry later" in message
    assert "litellm." not in message
    assert "\n" not in message
    assert message.endswith("(HTTP 429)")


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), ExceptionGroup("g", [ValueError("parse")])],
)
def test_non_provider_failures_are_not_provider_messages(exc: BaseException) -> None:
    assert provider_failure_message(exc, provider_label="OpenRouter") is None


def test_an_http_401_from_openrouter_is_not_a_codex_sign_in() -> None:
    """rel18: any error text holding "401"/"unauthorized" was reported as
    "Codex sign-in is required", whatever provider actually answered."""
    from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE

    raw = litellm.AuthenticationError(
        message='OpenrouterException - {"error":{"message":"User not found.","code":401}}',
        model="openrouter/free",
        llm_provider="openrouter",
    )
    exc = ExceptionGroup("unhandled errors in a TaskGroup", [_wrapped(raw)])

    detail = describe_stream_exc(exc, provider_id="openrouter")

    assert detail != CODEX_AUTHENTICATION_ERROR_MESSAGE
    assert failed_before_output(exc, detail, "openrouter") == (
        "OpenRouter: User not found. (HTTP 401)"
    )


def test_codex_sign_in_is_still_named_for_the_codex_provider() -> None:
    from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE

    exc = ExceptionGroup("g", [RuntimeError("unexpected status 401 Unauthorized")])

    assert describe_stream_exc(exc, provider_id="codex") == CODEX_AUTHENTICATION_ERROR_MESSAGE
