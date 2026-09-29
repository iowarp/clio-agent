"""``IOLoggingLM.acall`` keeps the sync path's provider contract (#1333).

The finalize GOAL judge is awaited (``Predict.acall`` -> ``LM.acall``), so the async
twin of ``__call__`` must apply the same LM Studio ``response_format`` shim, the same
bounded transient retry, and emit exactly one canonical ``lm.call`` capture per call —
otherwise the judge silently loses what every sync expert call has.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.lm import io_logging


def _fake_response(text: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop", logprobs=None)
    return SimpleNamespace(choices=[choice], usage={}, model="spy/model", _hidden_params={})


def _spy_lm(failures: list[BaseException] | None = None) -> Any:
    """An ``IOLoggingLM`` whose async transport is a recorder (no network, no sync path)."""

    base = io_logging._io_logging_lm_cls()

    class _Spy(base):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__(model="openai/spy-model", cache=False)
            self.async_kwargs: list[dict[str, Any]] = []
            self.sync_calls = 0
            self._failures = list(failures or [])

        def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
            self.sync_calls += 1
            raise AssertionError("acall must never fall back to the sync transport")

        async def aforward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
            self.async_kwargs.append(dict(kwargs))
            if self._failures:
                raise self._failures.pop(0)
            return _fake_response()

    return _Spy()


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "ping"}]


def test_acall_applies_the_lmstudio_response_format_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(io_logging, "_guided_output_enabled", lambda: True)
    lm = _spy_lm()

    out = asyncio.run(lm.acall(messages=_messages(), response_format={"type": "json_object"}))

    assert out and lm.sync_calls == 0
    sent = lm.async_kwargs[0]["response_format"]
    assert sent["type"] == "json_schema", sent
    assert sent["json_schema"]["schema"] == {"type": "object", "additionalProperties": True}


def test_acall_leaves_response_format_alone_when_guided_output_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(io_logging, "_guided_output_enabled", lambda: False)
    lm = _spy_lm()

    asyncio.run(lm.acall(messages=_messages(), response_format={"type": "json_object"}))

    assert lm.async_kwargs[0]["response_format"] == {"type": "json_object"}


def test_acall_retries_a_transient_provider_failure_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)
    lm = _spy_lm(failures=[ConnectionError("connection reset by peer")])

    out = asyncio.run(lm.acall(messages=_messages()))

    assert out and len(lm.async_kwargs) == 2


def test_acall_does_not_retry_a_non_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)
    lm = _spy_lm(failures=[ValueError("typed output did not parse")])

    with pytest.raises(ValueError):
        asyncio.run(lm.acall(messages=_messages()))
    assert len(lm.async_kwargs) == 1


def test_acall_does_not_retry_a_litellm_wrapped_claude_code_plan_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1529 regression: once LiteLLM re-wraps a Claude Code plan-limit hit as
    ``MidStreamFallbackError``/``APIConnectionError`` (its own documented
    behavior for an exception type it does not recognize, BerriAI/litellm#4201),
    BOTH class names are ``lm.io_logging`` transient markers on their own --
    so the retry layer used to silently re-issue a terminal plan-limit failure
    (with backoff), which is why the report says it "keeps retriggering" and
    can resurface after the user has already switched providers."""
    from types import SimpleNamespace

    from clio_agent.providers.claude_code_plan_limit import (
        PLAN_LIMIT_HTTP_STATUS,
        plan_limit_from_result,
    )

    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

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
    lm = _spy_lm(failures=[mangled])

    with pytest.raises(RuntimeError):
        asyncio.run(lm.acall(messages=_messages()))
    assert len(lm.async_kwargs) == 1  # never retried -- terminal until the window resets
