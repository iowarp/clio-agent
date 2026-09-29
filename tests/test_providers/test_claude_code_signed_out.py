"""#1454: provider refusals through the Claude Code engine are typed, terminal errors.

The SDK sequence below is the one recorded live (claude-agent-sdk 0.2.156, a
signed-out ``CLAUDE_CONFIG_DIR``): a synthetic ``AssistantMessage`` whose typed
``error`` is ``"authentication_failed"``, then a ``ResultMessage`` with
``is_error=True`` but ``subtype="success"`` and no ``api_error_status``. The old raise
printed the subtype, so the user read "...: success".

A sign-out, a 404 model rejection and an exhausted plan are clio's typed errors
(never retried -- re-issuing cannot succeed); an unclassified error result is a
retryable ``dspy.lm15`` server error that names the CLI's own words. Through
``ClioReAct`` each typed error reaches the turn as itself, not DSPy's wrapper.
"""

from __future__ import annotations

from collections.abc import Iterator

import dspy
import pytest
from dspy.lm15 import ServerError

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.lm.io_logging import _is_transient_provider_error
from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine, ClaudeCodeEngine
from clio_agent.providers.claude_code_errors import (
    CLAUDE_CODE_SIGNED_OUT_MESSAGE,
    ClaudeCodeSignedOutError,
    contains_claude_code_signed_out,
)
from clio_agent.providers.claude_code_plan_limit import ClaudeCodePlanLimitError
from clio_agent.providers.claude_code_result_errors import result_error_detail
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake

_NOT_LOGGED_IN = "Not logged in · Please run /login"


@pytest.fixture(autouse=True)
def _clean_pool() -> Iterator[None]:
    """A clean client pool and conversation registry per test."""
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def _error_result(result: str, *, api_error_status: int | None = None) -> fake.ResultMessage:
    return fake.ResultMessage(
        usage={"input_tokens": 0, "output_tokens": 0},
        result=result,
        is_error=True,
        api_error_status=api_error_status,
        subtype="success",
        stop_reason="stop_sequence",
    )


def _signed_out_turn() -> list[object]:
    return [
        fake.AssistantMessage(_NOT_LOGGED_IN, error="authentication_failed"),
        _error_result(_NOT_LOGGED_IN),
    ]


async def test_signed_out_result_raises_typed_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(monkeypatch, script=[_signed_out_turn()])

    with pytest.raises(ClaudeCodeSignedOutError) as excinfo:
        await fake.drive(fake.request(model="claude-sonnet-5"))

    text = str(excinfo.value)
    assert text.startswith(CLAUDE_CODE_SIGNED_OUT_MESSAGE)
    assert _NOT_LOGGED_IN in text  # the CLI's own words stay in the trace
    assert ": success" not in text
    assert excinfo.value.detail == _NOT_LOGGED_IN
    # Terminal: re-issuing cannot succeed until the user signs in again.
    assert _is_transient_provider_error(excinfo.value) is False


async def test_a_401_result_status_is_a_sign_out_too(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(
        monkeypatch, script=[[_error_result("Invalid bearer token", api_error_status=401)]]
    )

    with pytest.raises(ClaudeCodeSignedOutError):
        await fake.drive(fake.request())


async def test_a_404_result_is_a_typed_model_rejection_not_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    rejection = _error_result(
        "There's an issue with the selected model (claude-nope).", api_error_status=404
    )
    fake.install(monkeypatch, script=[[rejection]])

    with pytest.raises(litellm.BadRequestError) as excinfo:
        await fake.drive(fake.request(model="claude-nope"))

    assert "rejected model 'claude-nope'" in str(excinfo.value)
    assert "issue with the selected model" in str(excinfo.value)
    assert _is_transient_provider_error(excinfo.value) is False


async def test_an_exhausted_plan_is_the_typed_plan_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(monkeypatch, script=[[fake.RateLimitEvent("rejected")]])

    with pytest.raises(ClaudeCodePlanLimitError, match="usage limit reached"):
        await fake.drive(fake.request())


async def test_an_allowed_rate_limit_event_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, script=[[fake.RateLimitEvent("allowed"), *fake.answer("ok")]])
    response = await fake.drive(fake.request())
    assert response.message.parts[0].text == "ok"


async def test_unclassified_error_result_reports_the_cli_text_not_the_subtype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE-sensitive: an error result with no auth signal stays generic (a
    retryable server error), but names the CLI's own result text, not the subtype."""
    fake.install(
        monkeypatch,
        script=[
            [
                fake.AssistantMessage("API Error: overloaded", error="server_error"),
                _error_result("overloaded"),
            ]
        ],
    )

    with pytest.raises(ServerError) as excinfo:
        await fake.drive(fake.request(model="claude-sonnet-5"))

    assert not isinstance(excinfo.value, ClaudeCodeSignedOutError)
    assert str(excinfo.value).endswith("model=claude-sonnet-5: overloaded")


def test_the_loop_surfaces_a_sign_out_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSPy wraps an engine's own error as unexpected; ClioReAct re-raises the original."""
    fake.install(monkeypatch, script=[_signed_out_turn()])
    lm = dspy.LM(
        "claude_code/claude-sonnet-5",
        engine=ClaudeCodeEngine("claude-sonnet-5", cwd="/w", timeout=5.0),
        async_engine=AsyncClaudeCodeEngine("claude-sonnet-5", cwd="/w", timeout=5.0),
        cache=False,
        num_retries=0,
    )

    with dspy.context(lm=lm), pytest.raises(ClaudeCodeSignedOutError):
        ClioReAct("question -> answer", tools=[])(question="hi")


def test_result_error_detail_falls_back_to_status_then_subtype() -> None:
    assert result_error_detail(_error_result("  two\n words ")) == "two words"
    assert result_error_detail(_error_result("", api_error_status=500)) == "500"
    assert result_error_detail(_error_result("")) == "success"


def test_sign_out_survives_text_wrapping_and_groups() -> None:
    signed_out = ClaudeCodeSignedOutError(detail=_NOT_LOGGED_IN, model="claude-sonnet-5")
    wrapped = RuntimeError(f"dspy.LMUnexpectedError: {signed_out}")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [wrapped])

    assert contains_claude_code_signed_out(group)
    assert _is_transient_provider_error(wrapped) is False
    assert not contains_claude_code_signed_out(RuntimeError("dspy.LMUnexpectedError: 401"))
