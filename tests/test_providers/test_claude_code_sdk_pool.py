"""Tests for the blocking Claude Agent SDK session pool (iowarp/clio-agent#1211
review A3 / #1184) -- no dedicated test file existed for this module before.

Covers ``_SdkSession._aquery``'s model-rejection classification directly (via
``asyncio.run`` on the coroutine, bypassing the real background event-loop
thread ``_ensure_loop``/``_submit`` spin up) -- the blocking-path counterpart
to ``test_core/test_claude_code_provider.py``'s streaming-path coverage.
Before this change, a rejected model on this path silently degraded to the
generic "claude agent sdk returned empty content" error, discarding the
account's own rejection text and the 404 status entirely.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from clio_agent.providers.claude_code_sdk_pool import _SdkSession


class _FakeResultMessage:
    def __init__(self, *, is_error: bool, api_error_status: int | None, result: str) -> None:
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.result = result
        self.usage: dict[str, Any] | None = None


class _FakeAssistantMessage:
    """Marker type so isinstance(msg, AssistantMessage) never matches by accident."""


class _FakeClient:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.queried: list[str] = []

    async def query(self, prompt: str, session_id: str) -> None:
        self.queried.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for msg in self._messages:
            yield msg


def _fake_claude_sdk_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import ModuleType

    fake = ModuleType("claude_agent_sdk")
    fake.AssistantMessage = _FakeAssistantMessage  # type: ignore[attr-defined]
    fake.ResultMessage = _FakeResultMessage  # type: ignore[attr-defined]
    fake.TextBlock = type("FakeTextBlock", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def test_aquery_model_rejection_raises_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """iowarp/clio-agent#1184, #1211 review A3/D3 (failing-first): a definitive
    model rejection (is_error + api_error_status==404) on the BLOCKING path must
    raise litellm.BadRequestError carrying the account's own text -- never
    silently degrade to the generic "empty content" error."""
    import litellm

    _fake_claude_sdk_module(monkeypatch)
    session = _SdkSession()
    session._client = _FakeClient(
        [
            _FakeResultMessage(
                is_error=True,
                api_error_status=404,
                result="There's an issue with the selected model (bogus).",
            )
        ]
    )

    with pytest.raises(litellm.BadRequestError) as excinfo:
        asyncio.run(session._aquery("hello", model="bogus"))
    assert "issue with the selected model" in str(excinfo.value)


def test_aquery_non_rejection_error_status_returns_empty_not_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE-sensitive: a non-404 is_error status must NOT be classified as
    a rejection -- it falls through to the pre-existing empty-content path
    (handled by complete(), not _aquery itself)."""
    _fake_claude_sdk_module(monkeypatch)
    session = _SdkSession()
    session._client = _FakeClient(
        [_FakeResultMessage(is_error=True, api_error_status=500, result="server error")]
    )

    text, usage = asyncio.run(session._aquery("hello", model="bogus"))
    assert text == ""  # no AssistantMessage arrived -- complete() raises generically for this


def test_aquery_success_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal (non-error) turn is completely unaffected by the new check."""
    _fake_claude_sdk_module(monkeypatch)

    class _FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeAssistant(_FakeAssistantMessage):
        def __init__(self) -> None:
            self.content = [_FakeTextBlock("ok")]

    import sys

    sys.modules["claude_agent_sdk"].TextBlock = _FakeTextBlock  # type: ignore[attr-defined]

    session = _SdkSession()
    session._client = _FakeClient(
        [
            _FakeAssistant(),
            _FakeResultMessage(is_error=False, api_error_status=None, result="ok"),
        ]
    )
    text, usage = asyncio.run(session._aquery("hello", model="haiku"))
    assert text == "ok"
