"""Timer-driven idle reap for the claude_code streaming pool.

Regression: the release memory-budget gate (v0.9.4.18) measured three
``claude-sdk-cli`` processes still resident 180 s after load stopped. B1 keeps
one client per session, and the idle-TTL sweep only ran when ANOTHER session
asked for a connection, so once no new session arrived the last clients were
never reaped. :mod:`clio_agent.providers.claude_code_idle_reaper` sweeps on the
idle TTL itself.

Every test drives the REAL :class:`ClaudeStreamClientPool` against a fake
``claude_agent_sdk`` whose client owns a REAL child process (started on
``connect()``, terminated on ``disconnect()``), so "the CLI is gone" is checked
on an actual process handle. The idle TTL is set through its real knob
(``CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S``).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool

_TTL_S = 0.3
# Upper bound on how long a test waits for an expected state change. It only
# ends a FAILING test; a passing test returns as soon as the state is reached.
_WAIT_S = 10.0


class _FakeResultMessage:
    pass


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake SDK whose client starts a real child process on connect."""
    state: dict[str, Any] = {"constructed": 0, "disconnected": 0, "procs": []}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            self.proc: subprocess.Popen[bytes] | None = None

        async def connect(self) -> None:
            self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-only stand-in CLI
                [sys.executable, "-c", "import time; time.sleep(600)"]
            )
            state["procs"].append(self.proc)

        async def disconnect(self) -> None:
            state["disconnected"] += 1
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait()

        async def set_model(self, model: str | None) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            yield _FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = _FakeResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


@pytest.fixture
def short_ttl(monkeypatch: pytest.MonkeyPatch) -> Iterator[float]:
    """Set the real idle-TTL knob to a short value for the test."""
    monkeypatch.setenv("CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S", str(_TTL_S))
    conf.reload()
    yield _TTL_S
    monkeypatch.delenv("CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S", raising=False)
    conf.reload()


@pytest.fixture
def pool() -> Iterator[ClaudeStreamClientPool]:
    pool = ClaudeStreamClientPool(max_concurrent=4)
    yield pool
    pool.close_blocking()


def _wait_until(predicate: Any, *, timeout: float = _WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition never became true within {timeout}s")
        time.sleep(0.02)


def _turn(pool: ClaudeStreamClientPool, session_id: str) -> None:
    """Run one full streamed call for ``session_id`` through the pool."""

    async def _run() -> None:
        entry = pool.entry_for(session_id=session_id)
        async for _ in entry.stream(
            payload="hi",
            native_blocks=[],
            session_id=session_id,
            timeout=_WAIT_S,
            on_construct=pool.bump_construct,
            model="haiku",
        ):
            pass

    asyncio.run(_run())


def test_idle_client_is_reaped_on_the_timer_with_no_new_sessions(
    monkeypatch: pytest.MonkeyPatch,
    short_ttl: float,
    pool: ClaudeStreamClientPool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the last use plus the TTL, the client is closed and its process is gone.

    Nothing calls ``entry_for`` after the turn: only the reaper can reap it.

    SABOTAGE: drop ``self.wake_reaper()`` from ``entry_for`` and the ``on_idle``
    wake in ``_mark_idle`` -> the reaper never runs, the process survives -> red.
    """
    state = _install_fake_sdk(monkeypatch)
    caplog.set_level(logging.INFO, logger="clio_agent.providers.claude_code_idle_reaper")

    _turn(pool, "sess-a")
    (proc,) = state["procs"]
    assert proc.poll() is None  # the CLI stays resident between turns (B1)

    _wait_until(lambda: proc.poll() is not None)

    assert "sess-a" not in pool._entries
    assert state["disconnected"] == 1  # closed via disconnect(), not killed
    reaps = [r.getMessage() for r in caplog.records if "reason=idle_reaped" in r.getMessage()]
    assert reaps and "trigger=idle_timer" in reaps[0] and "session=sess-a" in reaps[0]


def test_a_later_turn_reconnects_after_a_timer_reap(
    monkeypatch: pytest.MonkeyPatch, short_ttl: float, pool: ClaudeStreamClientPool
) -> None:
    """A reaped session's next turn gets a fresh connection transparently."""
    state = _install_fake_sdk(monkeypatch)
    _turn(pool, "sess-a")
    first_proc = state["procs"][0]
    _wait_until(lambda: first_proc.poll() is not None)

    _turn(pool, "sess-a")

    assert state["constructed"] == 2
    assert state["procs"][1].poll() is None
    assert pool.entry_for(session_id="sess-a").dead is False


def test_a_busy_entry_is_never_reaped_by_the_timer(
    monkeypatch: pytest.MonkeyPatch, short_ttl: float, pool: ClaudeStreamClientPool
) -> None:
    """A call in flight has no idle deadline; the reaper leaves it alone."""
    _install_fake_sdk(monkeypatch)
    entry = pool.entry_for(session_id="sess-busy")
    entry._mark_busy()
    # Give the reaper several TTLs to (wrongly) act.
    time.sleep(_TTL_S * 5)
    assert pool._entries.get("sess-busy") is entry
    assert entry.dead is False

    entry._mark_idle()  # the call finishes: now it has a deadline
    _wait_until(lambda: "sess-busy" not in pool._entries)
    assert entry.dead is True


def test_an_unclaimed_precede_connect_is_reaped_on_the_timer(
    monkeypatch: pytest.MonkeyPatch, short_ttl: float, pool: ClaudeStreamClientPool
) -> None:
    """B2: a spare warm client that no turn ever claims is reaped on the same timer."""
    state = _install_fake_sdk(monkeypatch)
    pool.precede_connect(session_id="sess-warm", model="haiku")
    _wait_until(lambda: len(state["procs"]) == 1)
    proc = state["procs"][0]

    _wait_until(lambda: proc.poll() is not None)

    assert "sess-warm" not in pool._entries


def test_shutdown_stops_the_reaper(
    monkeypatch: pytest.MonkeyPatch, pool: ClaudeStreamClientPool
) -> None:
    """``close_blocking`` stops the reaper thread and closes every client."""
    state = _install_fake_sdk(monkeypatch)
    _turn(pool, "sess-a")  # default 15 s TTL: the client is still resident
    assert pool._reaper is not None and pool._reaper.running

    pool.close_blocking()

    assert not pool._reaper.running
    assert state["procs"][0].poll() is not None


def test_the_reaper_thread_exits_once_the_pool_is_empty(
    monkeypatch: pytest.MonkeyPatch, short_ttl: float, pool: ClaudeStreamClientPool
) -> None:
    """An empty pool holds no reaper thread; the next entry starts one again."""
    _install_fake_sdk(monkeypatch)
    _turn(pool, "sess-a")
    assert pool._reaper is not None
    _wait_until(lambda: not pool._entries and not pool._reaper.running)

    _turn(pool, "sess-b")
    assert pool._reaper.running
