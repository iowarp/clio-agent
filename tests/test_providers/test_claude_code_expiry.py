"""Claude Code waits on progress, never on a flat bound over the whole call (#1577 3.6).

* The streamed reply has no clock. A stall -- no message for the current wait
  (``limits.lm_stall_wait_s``, 30 s, longer at each stall) -- is shown to the person and
  retried: before any of the reply is on screen the query is resent; after, the same
  stream is waited on longer. Silence never fails the turn; the person stops it (#2211).
* The connect is bounded by the CLI process tree's own work: a CLI still working on a
  slow machine is waited for; one doing nothing fails typed ("connect").

Every test drives the REAL engine and pooled transport over the fake SDK; the connect
tests give the fake client a REAL child process as its CLI so the progress signal is
measured, not stubbed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest
from dspy.lm15 import TimeoutError as LMTimeoutError

from clio_agent.providers import (
    claude_code_conversation,
    claude_code_engine,
    claude_code_expiry,
    claude_code_stall,
)
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_conversation.forward_resets().clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_conversation.forward_resets().clear_for_tests()


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """The stall notices the person would see, in order."""
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        claude_code_stall, "_stall_notice", lambda text, **meta: seen.append((text, meta))
    )
    return seen


def _paced_client(monkeypatch: pytest.MonkeyPatch, gaps: list[float]) -> None:
    """Swap in a client whose reply waits ``gaps[i]`` before its i-th message."""
    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _Paced(base):  # type: ignore[misc, valid-type]
        async def receive_response(self) -> Any:
            for gap, message in zip(gaps, fake.answer("paced reply"), strict=False):
                await asyncio.sleep(gap)
                yield message

    monkeypatch.setattr(module, "ClaudeSDKClient", _Paced)


async def test_a_long_stream_that_keeps_streaming_completes(
    monkeypatch: pytest.MonkeyPatch, notices: list[Any]
) -> None:
    """Four messages 0.3 s apart (1.2 s overall) under a 0.6 s stall wait finish quietly.

    SABOTAGE: bound the whole query+receive by one ``asyncio.timeout`` again (the old
    flat DEFAULT_TIMEOUT_S shape) -> the call dies at 0.6 s -> red.
    """
    fake.install(monkeypatch)
    _paced_client(monkeypatch, [0.3, 0.3, 0.3, 0.3])
    response = await fake.drive(fake.request(), idle_timeout_s=0.6)
    assert response.message.parts[0].text == "paced reply"
    assert notices == []


async def test_a_stall_after_text_waits_longer_and_shows_it(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, dict[str, Any]]]
) -> None:
    """Text, then 1.0 s of silence under a 0.4 s first wait: a notice, a longer wait, done.

    SABOTAGE: fail on the first silent wait (the old 2-minute idle error) -> the call
    raises instead of completing -> red.
    """
    fake.install(monkeypatch)
    _paced_client(monkeypatch, [0.0, 1.0, 0.0, 0.0])
    response = await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=0.4), timeout=10)
    assert response.message.parts[0].text == "paced reply"
    assert notices, "the person must see the stall"
    text, meta = notices[0]
    assert meta["action"] == "wait" and meta["next_wait_s"] == pytest.approx(0.8)
    assert "Stop the turn to cancel" in text


async def test_a_stall_before_any_text_resends_with_a_longer_wait(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, dict[str, Any]]]
) -> None:
    """The first client never answers; the stall resends and the second client answers.

    SABOTAGE: raise the idle error instead of resending -> LM timeout -> red.
    """
    fake.install(monkeypatch)
    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]
    calls: list[int] = []

    class _FirstSilent(base):  # type: ignore[misc, valid-type]
        async def receive_response(self) -> Any:
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(30)
            for message in fake.answer("second try"):
                yield message

    monkeypatch.setattr(module, "ClaudeSDKClient", _FirstSilent)
    started = time.monotonic()
    response = await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=0.3), timeout=10)
    assert time.monotonic() - started < 10
    assert response.message.parts[0].text == "second try"
    assert len(calls) == 2
    [(text, meta)] = notices
    assert meta["action"] == "resend" and meta["retry_attempt"] == 2
    assert meta["next_wait_s"] == pytest.approx(0.6)
    assert "Retrying" in text


def test_the_stall_waits_grow_and_never_end() -> None:
    """30 s, then doubling, capped at :data:`STALL_MAX_WAIT_S`, for as long as needed."""
    waits = claude_code_expiry.stall_waits(30.0)
    assert [next(waits) for _ in range(7)] == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]


def test_the_first_stall_wait_defaults_to_the_stall_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit wait -> ``limits.lm_stall_wait_s`` (env override honoured)."""
    monkeypatch.setenv("CLIO_LM_STALL_WAIT_S", "42")
    engine = claude_code_engine.AsyncClaudeCodeEngine("haiku", cwd="/w")
    assert engine.idle_timeout() == 42.0
    monkeypatch.delenv("CLIO_LM_STALL_WAIT_S")
    assert claude_code_engine.AsyncClaudeCodeEngine("haiku", cwd="/w").idle_timeout() == 30.0


@pytest.fixture
def cli_process(request: pytest.FixtureRequest) -> Iterator[subprocess.Popen[bytes]]:
    """A real child process standing in for the Claude CLI (busy or idle)."""
    busy = request.param == "busy"
    code = "while True: pass" if busy else "import time; time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _slow_connect_client(monkeypatch: pytest.MonkeyPatch, pid: int, delay: float) -> None:
    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _SlowConnect(base):  # type: ignore[misc, valid-type]
        async def connect(self) -> None:
            # The SDK's own shape: the CLI subprocess hangs off the transport.
            self._transport = type("T", (), {"_process": type("P", (), {"pid": pid})()})()
            await asyncio.sleep(delay)

    monkeypatch.setattr(module, "ClaudeSDKClient", _SlowConnect)


@pytest.mark.parametrize("cli_process", ["busy"], indirect=True)
async def test_a_slow_connect_is_waited_for_while_the_cli_works(
    monkeypatch: pytest.MonkeyPatch, cli_process: subprocess.Popen[bytes]
) -> None:
    """A connect taking 1.5 s with a 0.3 s stretch succeeds: the CLI keeps working.

    SABOTAGE: bound the connect by ``first_wait_s`` alone (no progress check) -> the
    connect fails at 0.3 s -> red.
    """
    monkeypatch.setattr(claude_code_expiry, "CONNECT_FIRST_WAIT_S", 0.3)
    monkeypatch.setattr(claude_code_expiry, "CONNECT_STRETCH_S", 0.3)
    fake.install(monkeypatch)
    _slow_connect_client(monkeypatch, cli_process.pid, 1.5)
    response = await fake.drive(fake.request(), idle_timeout_s=5.0)
    assert response.message.parts[0].text == "Answer"


@pytest.mark.parametrize("cli_process", ["idle"], indirect=True)
async def test_a_connect_whose_cli_does_nothing_fails_typed(
    monkeypatch: pytest.MonkeyPatch, cli_process: subprocess.Popen[bytes]
) -> None:
    """A CLI doing no work for a whole stretch: a typed LM timeout naming the connect.

    SABOTAGE: treat "no progress" as progress (keep waiting) -> the 30 s connect runs
    out the wait_for below -> red.
    """
    monkeypatch.setattr(claude_code_expiry, "CONNECT_FIRST_WAIT_S", 0.3)
    monkeypatch.setattr(claude_code_expiry, "CONNECT_STRETCH_S", 0.5)
    fake.install(monkeypatch)
    _slow_connect_client(monkeypatch, cli_process.pid, 30.0)
    with pytest.raises(LMTimeoutError) as caught:
        await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=5.0), timeout=15)
    assert "connect" in str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, claude_code_expiry.ClaudeCodeConnectTimeout)
    assert cause.reason == "no_progress"


async def test_pings_after_text_do_not_hide_a_stall_and_stop_ends_it(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, dict[str, Any]]]
) -> None:
    """Text, then only the API's keep-alive pings: shown as a stall; cancelling ends it.

    The API sends a ``ping`` stream event every 30 s on an open stream; a stalled
    generation is nothing but pings. SABOTAGE: let a ping restart the wait -> no
    notice is ever posted -> red.
    """
    fake.install(monkeypatch)
    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _PingsOnly(base):  # type: ignore[misc, valid-type]
        async def receive_response(self) -> Any:
            yield fake.text("partial")
            for _ in range(200):
                await asyncio.sleep(0.1)
                yield fake.StreamEvent({"type": "ping"})

    monkeypatch.setattr(module, "ClaudeSDKClient", _PingsOnly)
    with pytest.raises(asyncio.TimeoutError):  # the person's Stop, standing in
        await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=0.3), timeout=2)
    assert notices and all(meta["action"] == "wait" for _, meta in notices)


async def test_the_relay_raises_idle_when_on_silence_declines() -> None:
    """``on_silence`` returning ``None`` ends the stream typed, for the engine's resend."""

    class _Silent:
        async def query(self, _input: Any, *, session_id: str) -> None:
            del session_id

        async def receive_response(self) -> Any:
            await asyncio.sleep(30)
            yield None

    started = time.monotonic()
    with pytest.raises(claude_code_expiry.ClaudeCodeIdleTimeout):
        await claude_code_expiry.relay_with_idle_bound(
            _Silent(),
            "q",
            session_id="s",
            idle_s=0.2,
            put=lambda _m: None,
            on_silence=lambda _s: None,
        )
    assert time.monotonic() - started < 5


async def test_the_relay_keeps_waiting_while_on_silence_extends() -> None:
    """``on_silence`` returning a wait puts a :class:`StreamSilence` and keeps the stream."""

    class _Late:
        async def query(self, _input: Any, *, session_id: str) -> None:
            del session_id

        async def receive_response(self) -> Any:
            await asyncio.sleep(0.5)
            yield "late reply"

    got: list[Any] = []
    await claude_code_expiry.relay_with_idle_bound(
        _Late(), "q", session_id="s", idle_s=0.2, put=got.append, on_silence=lambda _s: 0.2
    )
    silences = [m for m in got if isinstance(m, claude_code_expiry.StreamSilence)]
    assert silences and "late reply" in got


async def test_the_relay_ends_at_once_when_stopped_during_a_stall_wait() -> None:
    """Stop wakes a stall wait: it never waits for the stream or the wait to run out."""

    class _Silent:
        async def query(self, _input: Any, *, session_id: str) -> None:
            del session_id

        async def receive_response(self) -> Any:
            await asyncio.sleep(30)
            yield None

    stopped_at = time.monotonic() + 0.3
    started = time.monotonic()
    with pytest.raises(claude_code_expiry.ClaudeCodeStreamStopped):
        await claude_code_expiry.relay_with_idle_bound(
            _Silent(),
            "q",
            session_id="s",
            idle_s=20,
            put=lambda _m: None,
            on_silence=lambda _s: 20,
            stop=lambda: time.monotonic() >= stopped_at,
        )
    assert time.monotonic() - started < 3


async def test_stopping_a_stalled_turn_does_not_wait_out_the_stall_wait(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, dict[str, Any]]]
) -> None:
    """A stream that never answers, even to the interrupt: Stop still ends the call promptly.

    SABOTAGE: drop ``stop=`` from the pooled relay -> the call holds until the 20 s
    stall wait runs out -> red.
    """
    del notices
    fake.install(monkeypatch)
    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _NeverAnswers(base):  # type: ignore[misc, valid-type]
        async def receive_response(self) -> Any:
            await asyncio.sleep(60)
            yield fake.text("too late")

    monkeypatch.setattr(module, "ClaudeSDKClient", _NeverAnswers)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):  # the person's Stop, standing in
        await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=20), timeout=0.5)
    assert time.monotonic() - started < 5
