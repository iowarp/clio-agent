"""Integration tests for the Codex LiteLLM adapter's turn orchestration (A.9).

Covers the WS-pre-stream-failure -> SSE fallback, a 401 refreshing the
credential once and retrying, and cancel aborting a session's stream via the
shared ``claude_code_cancel`` registry.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from clio_agent.providers.claude_code_cancel import _reset_for_tests as reset_cancel_registry
from clio_agent.providers.claude_code_cancel import active_stream_sessions
from clio_agent.providers.codex import litellm_adapter
from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import CodexAuthError
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.sessions import reset_sessions_for_tests
from clio_agent.providers.codex.stream_events import Completed, TextDelta
from clio_agent.providers.codex.transport_ws import WsPreStreamFailure


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reset_sessions_for_tests()
    reset_cancel_registry()
    monkeypatch.setattr("clio_agent.gact.context.active_session_id", lambda: "test-session")
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    fresh_expiry = int(time.time() * 1000) + 60 * 60 * 1000
    store.save(
        CodexCredential(
            access_token="at", refresh_token="rt", expires_at_ms=fresh_expiry, account_id="a"
        )
    )
    litellm_adapter._reset_store_for_tests(store)
    yield
    litellm_adapter._reset_store_for_tests(None)
    reset_sessions_for_tests()
    reset_cancel_registry()


async def _sse_stream_ok(**_kwargs) -> AsyncIterator:
    yield TextDelta("hi")
    yield Completed(
        response_id="r1",
        output_items=[],
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def _ws_stream_fails_pre_stream(**_kwargs) -> AsyncIterator:
    raise WsPreStreamFailure("no route to host")
    yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_stream_turn_falls_back_to_sse_on_ws_pre_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm_adapter, "stream_ws_turn", _ws_stream_fails_pre_stream)
    monkeypatch.setattr(litellm_adapter, "stream_sse_turn", _sse_stream_ok)

    events = [
        e
        async for e in litellm_adapter.stream_turn(
            model="codex_direct/gpt-5.6-sol",
            messages=[{"role": "user", "content": "hi"}],
            params={},
        )
    ]
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(isinstance(e, Completed) for e in events)


@pytest.mark.asyncio
async def test_stream_turn_refreshes_once_on_401_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def ws_impl(**_kwargs) -> AsyncIterator:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CodexAuthError("access token rejected")
        yield Completed(response_id="r1", output_items=[], usage={})

    monkeypatch.setattr(litellm_adapter, "stream_ws_turn", ws_impl)

    refreshed = {"called": False}
    original_store = litellm_adapter._credential_store()

    def _get_valid_credential(*, force_refresh: bool = False):
        if force_refresh:
            refreshed["called"] = True
        return CodexCredential(
            access_token="at2", refresh_token="rt2", expires_at_ms=0, account_id="a"
        )

    monkeypatch.setattr(original_store, "get_valid_credential", _get_valid_credential)

    events = [
        e
        async for e in litellm_adapter.stream_turn(
            model="codex_direct/gpt-5.6-sol",
            messages=[{"role": "user", "content": "hi"}],
            params={},
        )
    ]
    assert calls["n"] == 2
    assert refreshed["called"] is True
    assert any(isinstance(e, Completed) for e in events)


@pytest.mark.asyncio
async def test_stream_turn_registers_and_unregisters_cancel_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm_adapter, "stream_ws_turn", _sse_stream_ok)

    seen_mid_stream: set[str] = set()

    async def _tracking_ws(**kwargs) -> AsyncIterator:
        seen_mid_stream.update(active_stream_sessions())
        yield Completed(response_id="r1", output_items=[], usage={})

    monkeypatch.setattr(litellm_adapter, "stream_ws_turn", _tracking_ws)

    async for _ in litellm_adapter.stream_turn(
        model="codex_direct/gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}], params={}
    ):
        pass
    assert "test-session" in seen_mid_stream
    # Unregistered once the turn completes -- no dangling handle for a finished turn.
    assert active_stream_sessions() == set()
