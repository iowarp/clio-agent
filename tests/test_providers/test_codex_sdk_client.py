"""The Codex SDK client: kept threads, typed thread-lost, compaction detection.

Pins :class:`clio_agent.providers.codex.sdk_client.CodexSDKClient` -- a kept thread is
continued with the next input, an unknown thread raises typed without tearing the
shared client down, a Codex compaction is reported once, and every thread opens with
the bare-LM config (no auto-compaction, no web search). The engine's conversation
semantics are pinned in ``test_codex_sdk_engine.py``. Every test fakes the
``openai_codex`` boundary; no real runtime or network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers.codex import sdk_client
from clio_agent.providers.codex.sdk_client import CodexThreadLostError


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex_home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.user_server]\ncommand = "x"\n', "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))


class _FakeTurn:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def stream(self) -> Any:
        for event in self._events:
            yield event

    async def interrupt(self) -> None:
        return None


class _FakeThread:
    def __init__(self, thread_id: str, events: list[Any]) -> None:
        self.id = thread_id
        self.inputs: list[Any] = []
        self._events = events

    async def turn(self, turn_input: Any, **_kw: Any) -> _FakeTurn:
        self.inputs.append(turn_input)
        return _FakeTurn(self._events)


def _fake_client_cls(events: list[Any], started: list[_FakeThread]) -> type:
    class _FakeClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            self.archived: list[str] = []

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def thread_start(self, **kwargs: Any) -> _FakeThread:
            config = kwargs["config"]
            assert (
                config["model_auto_compact_token_limit"] == sdk_client.NO_AUTO_COMPACT_TOKEN_LIMIT
            )
            # Codex's own web search is a hidden action clio never allows.
            assert config["web_search"] == "disabled"
            # The user's own servers are off in the THREAD, not only the runtime (found
            # live: a thread's config is re-derived from config.toml).
            assert config["mcp_servers"] == {"user_server": {"enabled": False}}
            thread = _FakeThread(f"thread-{len(started) + 1}", events)
            started.append(thread)
            return thread

        async def thread_archive(self, thread_id: str) -> None:
            self.archived.append(thread_id)

        async def close(self) -> None:
            return None

    return _FakeClient


def _delta(text: str) -> Any:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


async def _drain(client: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in client.stream(**kwargs)]


@pytest.mark.asyncio
async def test_client_keeps_a_thread_and_continues_it(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[_FakeThread] = []
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls([_delta("ok")], started))
    client = sdk_client.CodexSDKClient()
    opened: list[str] = []
    common = {"images": None, "model": "gpt-x", "cwd": None, "effort": None, "timeout": 30.0}
    try:
        await _drain(client, prompt="full", keep_thread=True, on_thread=opened.append, **common)
        await _drain(client, prompt="delta", thread_id="thread-1", **common)
        assert opened == ["thread-1"]
        assert len(started) == 1
        assert started[0].inputs == ["full", "delta"]
    finally:
        client.close_blocking()


@pytest.mark.asyncio
async def test_unknown_thread_raises_typed_and_keeps_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[_FakeThread] = []
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls([_delta("ok")], started))
    client = sdk_client.CodexSDKClient()
    common = {"images": None, "model": "gpt-x", "cwd": None, "effort": None, "timeout": 30.0}
    try:
        await _drain(client, prompt="warm", **common)
        generation = client._generation
        with pytest.raises(CodexThreadLostError):
            await _drain(client, prompt="delta", thread_id="thread-gone", **common)
        # A lost thread is not a broken runtime: the shared client is NOT torn down.
        await asyncio.sleep(0.05)
        assert client._generation == generation
        assert client._client is not None
    finally:
        client.close_blocking()


@pytest.mark.asyncio
async def test_codex_compaction_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[_FakeThread] = []
    compacted_event = SimpleNamespace(method="thread/compacted", payload=SimpleNamespace())
    events = [_delta("a"), compacted_event, compacted_event, _delta("b")]
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls(events, started))
    client = sdk_client.CodexSDKClient()
    calls: list[bool] = []
    try:
        await _drain(
            client,
            prompt="p",
            images=None,
            model="gpt-x",
            cwd=None,
            effort=None,
            timeout=30.0,
            on_compacted=lambda: calls.append(True),
        )
        assert calls == [True]
    finally:
        client.close_blocking()
