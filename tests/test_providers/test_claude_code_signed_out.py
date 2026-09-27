"""#1454: a signed-out Claude subscription is a typed, terminal provider error.

The SDK sequence below is the one recorded live (claude-agent-sdk 0.2.156, a
signed-out ``CLAUDE_CONFIG_DIR``): a synthetic ``AssistantMessage`` whose typed
``error`` is ``"authentication_failed"``, then a ``ResultMessage`` with
``is_error=True`` but ``subtype="success"`` and no ``api_error_status``. The old
raise printed the subtype, so the user read "...: success".
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from types import ModuleType
from typing import Any

import pytest

from clio_agent.lm.io_logging import _is_transient_provider_error
from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_errors import (
    CLAUDE_CODE_SIGNED_OUT_MESSAGE,
    ClaudeCodeSignedOutError,
    contains_claude_code_signed_out,
)
from clio_agent.providers.claude_code_litellm import ClaudeCodeExecError
from clio_agent.providers.claude_code_result_errors import result_error_detail

_NOT_LOGGED_IN = "Not logged in · Please run /login"


@pytest.fixture(autouse=True)
def _clean_pool() -> Iterator[None]:
    """A clean provider map and client pool per test (the pooled client would
    otherwise carry one test's fake SDK into the next)."""
    from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests

    claude_code_litellm._reset_for_tests()
    _reset_sessions_for_tests()
    yield
    claude_code_litellm._reset_for_tests()
    _reset_sessions_for_tests()


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(self, text: str, error: str | None) -> None:
        self.content = [_TextBlock(text)]
        self.error = error
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason = "stop_sequence"


class _ResultMessage:
    def __init__(self, result: str, *, api_error_status: int | None = None) -> None:
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason = "stop_sequence"
        self.result = result
        self.is_error = True
        self.api_error_status = api_error_status
        self.subtype = "success"


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClaudeSDKClient:
        def __init__(self, options: FakeClaudeAgentOptions) -> None:
            del options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            del prompt, session_id

        async def receive_response(self) -> AsyncIterator[Any]:
            for message in messages:
                yield message

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = _AssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
    fake_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    fake_sdk.ResultMessage = _ResultMessage
    fake_sdk.StreamEvent = type("FakeStreamEvent", (), {})
    fake_sdk.TextBlock = _TextBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)


async def _drain(model: str = "claude-sonnet-5") -> None:
    async for _ in claude_code_litellm._astream_sdk(
        prompt="Hello", model=model, timeout=5.0, cwd="/tmp/clio"
    ):
        pass


async def test_signed_out_result_raises_typed_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(
        monkeypatch,
        [
            _AssistantMessage(_NOT_LOGGED_IN, "authentication_failed"),
            _ResultMessage(_NOT_LOGGED_IN),
        ],
    )

    with pytest.raises(ClaudeCodeSignedOutError) as excinfo:
        await _drain()

    text = str(excinfo.value)
    assert text.startswith(CLAUDE_CODE_SIGNED_OUT_MESSAGE)
    assert _NOT_LOGGED_IN in text  # the CLI's own words stay in the trace
    assert ": success" not in text
    assert excinfo.value.detail == _NOT_LOGGED_IN
    # Terminal: re-issuing cannot succeed until the user signs in again.
    assert _is_transient_provider_error(excinfo.value) is False


async def test_a_401_result_status_is_a_sign_out_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, [_ResultMessage("Invalid bearer token", api_error_status=401)])

    with pytest.raises(ClaudeCodeSignedOutError):
        await _drain()


async def test_unclassified_error_result_reports_the_cli_text_not_the_subtype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE-sensitive: an error result with no auth signal stays generic,
    but names the CLI's own result text instead of ``subtype="success"``."""
    _install_fake_sdk(
        monkeypatch,
        [_AssistantMessage("API Error: overloaded", "server_error"), _ResultMessage("overloaded")],
    )

    with pytest.raises(ClaudeCodeExecError) as excinfo:
        await _drain()

    assert not isinstance(excinfo.value, ClaudeCodeSignedOutError)
    assert str(excinfo.value).endswith("model=claude-sonnet-5: overloaded")


def test_result_error_detail_falls_back_to_status_then_subtype() -> None:
    assert result_error_detail(_ResultMessage("  two\n words ")) == "two words"
    assert result_error_detail(_ResultMessage("", api_error_status=500)) == "500"
    assert result_error_detail(_ResultMessage("")) == "success"


def test_sign_out_survives_litellm_text_wrapping_and_groups() -> None:
    signed_out = ClaudeCodeSignedOutError(detail=_NOT_LOGGED_IN, model="claude-sonnet-5")
    wrapped = RuntimeError(f"litellm.APIConnectionError: {signed_out}")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [wrapped])

    assert contains_claude_code_signed_out(group)
    assert _is_transient_provider_error(wrapped) is False
    assert not contains_claude_code_signed_out(RuntimeError("litellm.APIConnectionError: 401"))
