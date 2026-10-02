"""The sync and async Claude Code engines ride the ONE session-pooled client (S2 B1).

``ClaudeCodeEngine`` (sync) drives the same async query on a private loop, so a sync
call and an async call in the same GACT session share one pooled, connected client;
and the factory builds the engine LM from the per-LM config alone -- the retired
process-global ``CLIO_CLAUDE_CODE_TRANSPORT`` env var is never consulted.
"""

from __future__ import annotations

from typing import Any

import pytest
from dspy.lm15 import Message, Request

from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine, ClaudeCodeEngine
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def test_sync_and_async_calls_share_one_pooled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    sdk = fake.install(monkeypatch)
    request = Request(model="claude_code/haiku", messages=(Message.user("hi"),))

    sync = ClaudeCodeEngine("haiku", cwd="/w", idle_timeout_s=5.0).complete(request)
    async_ = asyncio.run(
        AsyncClaudeCodeEngine("haiku", cwd="/w", idle_timeout_s=5.0).complete(request)
    )

    assert sync.message.parts == async_.message.parts
    # SABOTAGE: give the sync twin its own client -> 2 -> red.
    assert sdk.constructed == 1
    assert len(sdk.queries()) == 2


def test_the_factory_builds_the_engine_lm_and_ignores_the_transport_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    monkeypatch.setenv("CLIO_CLAUDE_CODE_TRANSPORT", "exec")
    lm = create_lm(LMProviderConfig(provider="claude_code", model="claude-sonnet-4-6"))

    assert lm.model == "claude_code/claude-sonnet-4-6"
    assert isinstance(lm._engine_spec, ClaudeCodeEngine)
    assert isinstance(lm._async_engine_spec, AsyncClaudeCodeEngine)
    assert not any(key.startswith("claude_code_") for key in lm.kwargs)
