"""Claude Code waits on progress, never on a flat bound over the whole call (#1577 3.6).

* The streamed reply is bounded by the gap BETWEEN messages
  (``limits.lm_inter_token_idle_s``), so a long answer that keeps streaming finishes
  however long it takes, and a stream that goes silent fails fast and typed ("idle").
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

from clio_agent.providers import claude_code_engine, claude_code_expiry
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four messages 0.3 s apart (1.2 s overall) under a 0.6 s idle bound finish.

    SABOTAGE: bound the whole query+receive by one ``asyncio.timeout`` again (the old
    flat DEFAULT_TIMEOUT_S shape) -> the call dies at 0.6 s -> red.
    """
    fake.install(monkeypatch)
    _paced_client(monkeypatch, [0.3, 0.3, 0.3, 0.3])
    response = await fake.drive(fake.request(), idle_timeout_s=0.6)
    assert response.message.parts[0].text == "paced reply"


async def test_a_silent_stream_fails_typed_as_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """One message, then silence: a typed LM timeout naming the idle gap, raised fast.

    SABOTAGE: drop the idle bound (no timeout around the receive loop) -> the call hangs
    for the full 30 s gap -> the wait_for below fails -> red.
    """
    fake.install(monkeypatch)
    _paced_client(monkeypatch, [0.0, 30.0])
    started = time.monotonic()
    with pytest.raises(LMTimeoutError) as caught:
        await asyncio.wait_for(fake.drive(fake.request(), idle_timeout_s=0.4), timeout=10)
    assert time.monotonic() - started < 10
    text = str(caught.value)
    assert "idle" in text and "1 message" in text
    assert isinstance(caught.value.__cause__, claude_code_expiry.ClaudeCodeIdleTimeout)


def test_the_idle_bound_defaults_to_the_inter_token_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit bound -> ``limits.lm_inter_token_idle_s`` (env override honoured)."""
    monkeypatch.setenv("CLIO_LM_INTER_TOKEN_IDLE_S", "42")
    engine = claude_code_engine.AsyncClaudeCodeEngine("haiku", cwd="/w")
    assert engine.idle_timeout() == 42.0


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
