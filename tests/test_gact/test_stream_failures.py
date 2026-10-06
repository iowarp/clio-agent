"""A failed turn's error.

Provider HTTP errors reach the user as one plain line in the provider's own words
(rel18 live finding); a refused sign-in or install failure of the configured CLI
provider as its stable message; anything else as ``agent_error`` naming the real
cause.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dspy
import litellm
import pytest

from clio_agent.gact.stream_failures import (
    forward_error_info,
    provider_auth_failure,
    provider_failure_message,
    turn_failure_message,
)

_OPENROUTER_BODY = '{"error":{"message":"No endpoints available for openrouter/free","code":404}}'


def _state(provider_id: str) -> Any:
    """The slice of ``TurnState`` ``forward_error_info`` reads: the turn's route."""

    return SimpleNamespace(
        user_msg=SimpleNamespace(
            metadata={"effective_model": {"provider_id": provider_id, "model_id": "m"}}
        )
    )


def _wrapped(raw: Exception) -> Exception:
    """Wrap ``raw`` exactly the way ``dspy.LM`` does at the LiteLLM boundary."""

    wrapped = dspy.LM("openrouter/openrouter/free", api_key="test")._wrap_litellm_exception(raw)
    wrapped.__cause__ = raw
    return wrapped


def _not_found() -> Exception:
    return _wrapped(
        litellm.NotFoundError(
            message=f"OpenrouterException - {_OPENROUTER_BODY}",
            model="openrouter/free",
            llm_provider="openrouter",
        )
    )


def test_provider_error_is_one_plain_line_in_the_providers_own_words() -> None:
    exc = _not_found()

    message = provider_failure_message(exc, provider_label="OpenRouter")

    assert message == "OpenRouter: No endpoints available for openrouter/free (HTTP 404)"
    info = forward_error_info(_state("openrouter"), exc, "")
    assert info.error == "provider_error"
    assert info.message == message
    assert info.recoverable is True
    # The details keep the real exception type for the trace.
    assert info.details == {"original_error": type(exc).__name__, "partial_output": False}


def test_a_provider_error_inside_a_group_or_cause_chain_is_still_found() -> None:
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [_not_found()])
    chained = RuntimeError("tool loop failed")
    chained.__cause__ = _not_found()

    for exc in (grouped, chained):
        assert provider_failure_message(exc, provider_label="OpenRouter") == (
            "OpenRouter: No endpoints available for openrouter/free (HTTP 404)"
        )


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
    exc = _wrapped(raw)

    info = forward_error_info(_state("openrouter"), exc, "")

    assert info.message != CODEX_AUTHENTICATION_ERROR_MESSAGE
    assert info.message == "OpenRouter: User not found. (HTTP 401)"
    assert turn_failure_message(exc, provider_id="openrouter", otherwise="x") == info.message


def test_codex_sign_in_is_still_named_for_the_codex_provider() -> None:
    from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE

    info = forward_error_info(
        _state("codex"), RuntimeError("unexpected status 401 Unauthorized"), ""
    )

    assert info.error == "provider_error"
    assert info.message == CODEX_AUTHENTICATION_ERROR_MESSAGE
    assert info.details["reason"] == "provider_auth_required"
    assert info.details["provider_id"] == "codex"


def _signed_out() -> Exception:
    from clio_agent.providers.claude_code_errors import ClaudeCodeSignedOutError

    signed_out = ClaudeCodeSignedOutError(detail="Not logged in", model="claude-sonnet-5")
    return RuntimeError(f"litellm.APIConnectionError: {signed_out}")


def test_claude_sign_out_line_is_scoped_to_the_configured_provider() -> None:
    assert provider_auth_failure(_signed_out(), provider_id="openrouter") is None
    assert provider_auth_failure(_signed_out(), provider_id="") is None


def test_sign_out_is_a_typed_provider_error_naming_the_sign_in_provider() -> None:
    from clio_agent.providers.claude_code_errors import CLAUDE_CODE_SIGNED_OUT_MESSAGE

    info = forward_error_info(_state("claude_code"), _signed_out(), "partial")

    assert info.error == "provider_error"
    assert info.message == CLAUDE_CODE_SIGNED_OUT_MESSAGE
    assert info.recoverable is True
    assert info.details["reason"] == "provider_auth_required"
    assert info.details["provider_id"] == "claude_code"
    assert info.details["provider_label"] == "Claude Code"
    assert info.details["recovery_actions"] == ["provider_sign_in", "retry"]
    assert info.details["original_error"] == "RuntimeError"
    assert info.details["partial_output"] is True


def test_claude_code_install_failure_is_its_stable_message() -> None:
    from clio_agent.providers.claude_code_errors import CLAUDE_CODE_INSTALL_FAILED_MESSAGE

    exc = ModuleNotFoundError("No module named 'claude_agent_sdk'")

    info = forward_error_info(_state("claude_code"), exc, "")

    assert info.error == "provider_error"
    assert info.message == CLAUDE_CODE_INSTALL_FAILED_MESSAGE
    # Scoped: the same exception on another provider is not a Claude Code install.
    assert forward_error_info(_state("openrouter"), exc, "").error == "agent_error"


def test_any_other_failure_is_an_agent_error_naming_the_real_cause() -> None:
    info = forward_error_info(_state("openrouter"), ValueError("parse failed"), "")

    assert info.error == "agent_error"
    assert info.message == "agent.forward raised: parse failed"
    assert info.recoverable is True
    assert info.details == {"original_error": "ValueError", "partial_output": False}


def _plan_limit_group() -> tuple[ExceptionGroup, str]:
    """A Claude Code plan-limit hit, LiteLLM-mangled the way it reaches the
    trace live (BerriAI/litellm#4201: an unrecognized exception type's
    ``str()`` + a full traceback baked into the wrapper's own message) --
    the shape ``clio_agent.providers.claude_code_plan_limit``'s own test
    module proves in full fidelity against real ``litellm``/``dspy`` classes;
    this mirrors ``_signed_out_group``'s simpler plain-``RuntimeError`` style
    since only the text and tree shape matter here.
    """
    from types import SimpleNamespace

    from clio_agent.providers.claude_code_plan_limit import (
        PLAN_LIMIT_HTTP_STATUS,
        plan_limit_from_result,
    )

    clean = plan_limit_from_result(
        SimpleNamespace(is_error=True, api_error_status=PLAN_LIMIT_HTTP_STATUS, result=""),
        model="claude-sonnet-5",
    )
    assert clean is not None
    mangled = RuntimeError(
        f"litellm.MidStreamFallbackError: litellm.APIConnectionError: {clean}\n"
        "Traceback (most recent call last):\n"
        '  File ".../claude_code_litellm.py", line 448, in _process\n'
        "    raise plan_limit\n"
        f"clio_agent.providers.claude_code_plan_limit.ClaudeCodePlanLimitError: {clean}\n"
    )
    return ExceptionGroup("unhandled errors in a TaskGroup", [mangled]), str(clean)


def test_plan_limit_is_one_clean_line_regardless_of_the_configured_provider() -> None:
    """#1529 live evidence: the session had already been switched to Codex when
    a Claude Code plan-limit hit surfaced its raw traceback. Unlike the auth
    detectors (deliberately scoped to the configured provider), a plan-limit
    signal is CLIO's own unambiguous marker text, so it must be recognized no
    matter what the turn's OWN configured provider was."""
    group, clean = _plan_limit_group()

    message = turn_failure_message(group, provider_id="codex", otherwise="should not be used")

    assert message == clean
    assert "Traceback" not in message
    assert "claude-sonnet-5" in message


def test_plan_limit_is_a_typed_provider_error() -> None:
    from clio_agent.gact.stream_failures import CLAUDE_CODE_PLAN_LIMIT_REASON

    group, clean = _plan_limit_group()

    info = forward_error_info(_state("codex"), group, "")

    assert info.error == "provider_error"
    assert info.message == clean
    assert info.details["reason"] == CLAUDE_CODE_PLAN_LIMIT_REASON
    assert info.details["model"] == "claude-sonnet-5"
    assert info.details["rate_limit_type"] is None  # the 429 result path carries none
