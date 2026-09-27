"""S2 Claude SDK tuning: B13 (live model switch), B14 (interrupt, not
disconnect), B2 (session-open precede-connect), B17 (dead client replaced
with a fresh mint).

Each pin drives the REAL :class:`_StreamClientEntry`/:class:`ClaudeStreamClientPool`
against a fake ``claude_agent_sdk`` module — no mocking of the code under test
itself. B2's own precede-connect pins (cap/eviction/first-turn reuse/failure
isolation) live in ``test_claude_code_precede_connect.py``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import ModuleType
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.providers.claude_code_cancel import abort_session_streams
from clio_agent.providers.claude_code_sessions import (
    ClaudeStreamClientPool,
    _reset_sessions_for_tests,
    _StreamClientEntry,
)


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    yield
    _reset_sessions_for_tests()


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "constructed": 0,
        "connected": 0,
        "disconnected": 0,
        "interrupted": 0,
        "set_model_calls": [],
        "clients": [],
    }

    class FakeResultMessage:
        pass

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            self.options = options
            self.interrupted = asyncio.Event()
            state["clients"].append(self)

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            state["disconnected"] += 1

        async def interrupt(self) -> None:
            state["interrupted"] += 1
            self.interrupted.set()

        async def set_model(self, model: str | None) -> None:
            state["set_model_calls"].append(model)

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = FakeResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


def _install_interruptible_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Like :func:`_install_fake_sdk`, but ``receive_response`` blocks on the
    client's own ``interrupted`` event after one chunk -- exactly the SDK's
    documented contract: an interrupted turn still yields a normal terminal
    message and ``receive_response()`` completes cleanly."""
    state = _install_fake_sdk(monkeypatch)
    import claude_agent_sdk as fake_sdk  # noqa: PLC0415

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    async def receive_response(self: Any) -> Any:
        yield FakeStreamEvent(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}
        )
        await self.interrupted.wait()
        yield fake_sdk.ResultMessage()

    fake_sdk.ClaudeSDKClient.receive_response = receive_response
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    fake_sdk.AssistantMessage = type("AssistantMessage", (), {})
    return state


async def _consume(entry: _StreamClientEntry, **kwargs: Any) -> list[Any]:
    out: list[Any] = []
    async for msg in entry.stream(**kwargs):
        out.append(msg)
    return out


# --------------------------------------------------------------------------- #
# B14: cancel interrupts a live client; it is never disconnected, and the
# NEXT turn on the same session reuses it warm.
# --------------------------------------------------------------------------- #
async def test_cancel_interrupts_the_client_and_it_stays_warm_for_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: register ``entry._areset_client`` (disconnect) as the cancel
    callback instead of ``request_interrupt`` -> ``state["disconnected"]``
    becomes 1 and the second call below reconnects (constructed == 2) -> red.
    """
    state = _install_interruptible_fake_sdk(monkeypatch)
    entry = _StreamClientEntry()
    token = gact_context.set_session_id("sess-cancel")
    try:
        task = asyncio.create_task(
            _consume(
                entry,
                payload="p1",
                native_blocks=[],
                session_id="sid-1",
                timeout=5.0,
                on_construct=lambda: None,
                model="haiku",
            )
        )
        # Wait for the client to CONNECT (the first connect also resolves which
        # CLI to pin, a one-time probe), then let the first chunk arrive.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while state["connected"] < 1 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        killed = abort_session_streams("sess-cancel")
        assert killed == 1
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        gact_context.reset(token)

    assert state["interrupted"] == 1
    assert state["disconnected"] == 0  # never disconnected on cancel
    assert entry._dead is False

    # The NEXT turn on the same session reuses the SAME (still-connected) client.
    await _consume(
        entry,
        payload="p2",
        native_blocks=[],
        session_id="sid-2",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
    )
    assert state["constructed"] == 1  # SABOTAGE target: a reconnect -> 2 -> red


async def test_cancel_before_the_query_starts_abandons_without_ever_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B14: registration covers the WHOLE call, including a still-queued
    connect -- a cancel that lands before any client exists must abandon the
    wait, never silently proceed to connect a client nobody wants anymore.
    """
    import threading

    state = _install_fake_sdk(monkeypatch)
    slots = threading.Semaphore(0)  # never available -- the connect stays queued
    entry = _StreamClientEntry(connect_slots=slots)
    token = gact_context.set_session_id("sess-queued")
    try:
        task = asyncio.create_task(
            _consume(
                entry,
                payload="p",
                native_blocks=[],
                session_id="sid",
                timeout=None,
                on_construct=lambda: None,
                model="haiku",
            )
        )
        await asyncio.sleep(0.05)  # let it genuinely start queueing for the slot
        assert not task.done()
        killed = abort_session_streams("sess-queued")
        assert killed == 1
        from clio_agent.providers.claude_code_lifecycle import StreamAbandonedError

        with pytest.raises(StreamAbandonedError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        gact_context.reset(token)

    assert state["constructed"] == 0  # never even reached ClaudeSDKClient(...)


# --------------------------------------------------------------------------- #
# B13: a model-only change on an existing entry is a live ``set_model`` call,
# never a reconnect.
# --------------------------------------------------------------------------- #
async def test_model_switch_uses_set_model_without_a_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_sdk(monkeypatch)
    entry = _StreamClientEntry()

    await _consume(
        entry,
        payload="p1",
        native_blocks=[],
        session_id="sid-1",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
    )
    await _consume(
        entry,
        payload="p2",
        native_blocks=[],
        session_id="sid-2",
        timeout=5.0,
        on_construct=lambda: None,
        model="sonnet",
    )

    assert state["constructed"] == 1  # SABOTAGE: reconnect on model change -> 2 -> red
    assert state["set_model_calls"] == ["sonnet"]


async def test_cwd_change_never_forces_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cwd`` is a bare-model-transport no-op (no tools/settings/hooks ever
    resolve a relative path against it here -- B3/B5/B6/B9 disable all of
    them), so a differing ``cwd`` alone must never trigger a reconnect --
    unlike ``thinking``/``system_prompt``, which genuinely cannot be applied
    to a live connection in this SDK version.

    SABOTAGE: put ``cwd`` back into the reconnect-mismatch check -> this call
    reconnects -> ``constructed`` goes to 2 -> red.
    """
    state = _install_fake_sdk(monkeypatch)
    entry = _StreamClientEntry()

    await _consume(
        entry,
        payload="p1",
        native_blocks=[],
        session_id="sid-1",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
        cwd="/workspace/a",
    )
    await _consume(
        entry,
        payload="p2",
        native_blocks=[],
        session_id="sid-2",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
        cwd="/workspace/b",
    )

    assert state["constructed"] == 1


# --------------------------------------------------------------------------- #
# B17: a dead entry's session is replaced with a fresh mint, typed and logged.
# --------------------------------------------------------------------------- #
def test_dead_entry_is_replaced_with_a_fresh_mint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = ClaudeStreamClientPool(max_concurrent=4)

    dead_entry = pool.entry_for(session_id="sess-x")
    dead_entry._dead = True

    with caplog.at_level(logging.WARNING):
        replacement = pool.entry_for(session_id="sess-x")

    assert replacement is not dead_entry
    assert replacement.dead is False
    assert any("dead_client_replaced" in rec.getMessage() for rec in caplog.records)


async def test_a_clean_stream_end_never_burns_the_entry_when_the_owner_loop_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-only flake of the two warm-reuse pins (4 xdist workers on 4 vCPUs).

    ``_pump`` queues the END and only then returns, so the owner-loop future is marked
    done a moment AFTER the caller already has the END. A caller that reached its
    ``finally`` inside that gap saw ``not fut.done()``, took the stream for abandoned,
    and burned a healthy entry (``_mark_dead_and_reset``): the next turn reconnected
    (constructed == 2) or refused (``released during a queued connect``). Here the owner
    loop is held right after the last message, exactly where a loaded runner stalls it.
    """
    import time  # noqa: PLC0415

    state = _install_fake_sdk(monkeypatch)
    import claude_agent_sdk as fake_sdk  # noqa: PLC0415

    async def receive_response(self: Any) -> Any:
        yield fake_sdk.ResultMessage()
        # Queued ahead of the future's completion callback: the owner loop stalls with
        # the END already delivered and `fut` not yet done.
        asyncio.get_running_loop().call_soon(time.sleep, 0.5)

    fake_sdk.ClaudeSDKClient.receive_response = receive_response
    entry = _StreamClientEntry()
    kwargs: dict[str, Any] = {
        "native_blocks": [],
        "timeout": 5.0,
        "on_construct": lambda: None,
        "model": "haiku",
    }
    await _consume(entry, payload="p1", session_id="sid-1", **kwargs)
    await asyncio.sleep(0.8)  # let any (wrongly) scheduled reset land before looking

    assert entry.dead is False, "a cleanly finished stream burned its entry"
    await _consume(entry, payload="p2", session_id="sid-2", **kwargs)
    assert state["constructed"] == 1  # the same warm client served both turns
    assert state["disconnected"] == 0


async def test_a_failed_stream_is_reset_once_when_the_owner_loop_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error-path twin (CI: test_pump_queues_stream_end_before_the_abnormal_end_reset
    recorded ``['end', 'reset', 'reset']``). ``_pump`` resets an abnormally ended stream
    itself; a caller that got the error while the owner loop was still finishing saw
    ``not fut.done()`` and scheduled a SECOND reset of the same entry."""
    import time  # noqa: PLC0415

    state = _install_fake_sdk(monkeypatch)
    import claude_agent_sdk as fake_sdk  # noqa: PLC0415

    async def query(self: Any, prompt: str, session_id: str = "default") -> None:
        # Stall the owner loop after `_pump` finishes, before its future is marked done.
        asyncio.get_running_loop().call_soon(time.sleep, 0.5)
        raise RuntimeError("boom")

    fake_sdk.ClaudeSDKClient.query = query
    entry = _StreamClientEntry()
    resets: list[int] = []
    real_areset = entry._areset_client

    async def counting_areset() -> None:
        resets.append(1)
        await real_areset()

    monkeypatch.setattr(entry, "_areset_client", counting_areset)
    with pytest.raises(RuntimeError, match="boom"):
        await _consume(
            entry,
            payload="p1",
            native_blocks=[],
            session_id="sid-1",
            timeout=5.0,
            on_construct=lambda: None,
            model="haiku",
        )
    await asyncio.sleep(0.8)  # let any (wrongly) scheduled second reset land

    assert entry.dead is True  # the failed stream burned its entry, as designed
    assert len(resets) == 1, "the failed stream was reset more than once"
    assert state["disconnected"] == 1
