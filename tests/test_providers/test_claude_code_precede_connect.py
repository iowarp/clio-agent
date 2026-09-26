"""B2: session-open precede-connect (S2 Claude SDK tuning).

:meth:`~clio_agent.providers.claude_code_sessions.ClaudeStreamClientPool
.precede_connect` replaces the generic bare-config warm pool: a caller that
already has a session's REAL resolved ``model``/``cwd``/``thinking``/
``system_prompt`` (session create, or first resolution to a ``claude_code``
model) kicks off a background connect for THAT session's own entry, before
any turn asks for it. This module pins the four behaviours the owner
explicitly scoped for this slice: the real config is used, the first turn
reuses the pre-connected entry in a single connect, a pre-connect failure
never reaches the turn, and the pending-connect count is capped so a burst of
session creates can never queue ahead of a real turn's own connect.

Each pin drives the REAL :class:`ClaudeStreamClientPool` against a fake
``claude_agent_sdk`` module -- no mocking of the code under test itself.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from types import ModuleType
from typing import Any

import pytest

from clio_agent.providers import claude_code_sessions as ccs
from clio_agent.providers import claude_code_stream_bounds as csb
from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool


class _FakeResultMessage:
    pass


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"constructed": 0, "connected": 0, "options": []}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            state["options"].append(options)

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            pass

        async def set_model(self, model: str | None) -> None:
            pass

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


def _wait_until(predicate: Any, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Poll ``predicate`` instead of a fixed sleep (#1305: no sleep-vs-poll races)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition never became true within {timeout}s")
        time.sleep(interval)


async def _consume(entry: Any, **kwargs: Any) -> None:
    async for _ in entry.stream(**kwargs):
        pass


# --------------------------------------------------------------------------- #
# The real config reaches the connect.
# --------------------------------------------------------------------------- #
def test_precede_connect_mints_an_entry_and_connects_with_the_real_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_sdk(monkeypatch)
    pool = ClaudeStreamClientPool(max_concurrent=4)

    pool.precede_connect(
        session_id="sess-open",
        model="sonnet",
        cwd="/workspace/proj",
        thinking={"type": "enabled", "budget_tokens": 1024},
        system_prompt="You are CLIO.",
    )
    # Wait for the background attempt to fully finish (not just the connect
    # call) so the entry's post-connect bookkeeping is guaranteed visible.
    _wait_until(lambda: "sess-open" not in pool._precede_pending)
    assert state["connected"] == 1

    entry = pool._entries["sess-open"]
    assert entry._model == "sonnet"
    assert entry._cwd == "/workspace/proj"
    assert entry._system_prompt == "You are CLIO."
    assert entry._thinking == {"type": "enabled", "budget_tokens": 1024}


def test_precede_connect_is_a_noop_for_an_empty_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No active GACT session (off-turn) must never mint a shared-key entry."""
    state = _install_fake_sdk(monkeypatch)
    pool = ClaudeStreamClientPool(max_concurrent=4)

    pool.precede_connect(session_id="", model="haiku")

    assert pool._entries == {}
    assert state["constructed"] == 0


def test_precede_connect_is_a_noop_when_the_session_already_has_an_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session already claimed (or already pre-connecting) is never re-minted."""
    state = _install_fake_sdk(monkeypatch)
    pool = ClaudeStreamClientPool(max_concurrent=4)
    existing = pool.entry_for(session_id="sess-existing")

    pool.precede_connect(session_id="sess-existing", model="haiku")

    assert pool._entries["sess-existing"] is existing
    assert state["constructed"] == 0  # no second entry minted, no connect attempted


# --------------------------------------------------------------------------- #
# The first real turn reuses the pre-connected entry -- ONE connect, not two.
# --------------------------------------------------------------------------- #
async def test_first_turn_reuses_the_precede_connected_entry_with_a_single_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: have ``entry_for`` mint a fresh entry instead of returning the
    pre-connected one -> the real turn below reconnects -> ``constructed``
    goes to 2 -> red.
    """
    state = _install_fake_sdk(monkeypatch)
    pool = ClaudeStreamClientPool(max_concurrent=4)

    pool.precede_connect(session_id="sess-reuse", model="haiku", system_prompt="Hi.")
    _wait_until(lambda: "sess-reuse" not in pool._precede_pending)
    assert state["connected"] == 1
    precede_entry = pool._entries["sess-reuse"]

    claimed = pool.entry_for(session_id="sess-reuse")
    assert claimed is precede_entry

    await _consume(
        claimed,
        payload="p",
        native_blocks=[],
        session_id="sid",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
        system_prompt="Hi.",
    )
    assert state["constructed"] == 1  # the pre-connect's client, reused -- no second connect


async def test_first_turn_with_a_drifted_config_reconnects_via_the_existing_typed_path(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """If the resolved config changed between session-open and the first turn,
    the pre-existing typed reconnect (not new precede-connect logic) handles
    it -- this is the module's own documented contract, pinned here so a
    regression in the wiring is caught at the precede-connect call site too.
    """
    state = _install_fake_sdk(monkeypatch)
    pool = ClaudeStreamClientPool(max_concurrent=4)

    pool.precede_connect(session_id="sess-drift", model="haiku", system_prompt=None)
    _wait_until(lambda: "sess-drift" not in pool._precede_pending)
    assert state["connected"] == 1

    claimed = pool.entry_for(session_id="sess-drift")
    with caplog.at_level(logging.INFO):
        await _consume(
            claimed,
            payload="p",
            native_blocks=[],
            session_id="sid",
            timeout=5.0,
            on_construct=lambda: None,
            model="haiku",
            system_prompt="A real prompt now.",
        )
    assert state["constructed"] == 2  # reconnected once, typed
    assert any("config_change_requires_restart" in rec.getMessage() for rec in caplog.records)


# --------------------------------------------------------------------------- #
# A pre-connect failure never reaches the turn.
# --------------------------------------------------------------------------- #
async def test_precede_connect_failure_leaves_the_first_turn_to_connect_normally(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """The pre-connect's OWN attempt fails (e.g. the CLI could not spawn);
    the entry is left in place with no client, and the session's actual first
    turn connects it cold -- exactly as if pre-connect had never run.
    """
    attempts = {"n": 0}

    class FlakyOptions:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FlakyClient:
        def __init__(self, options: FlakyOptions) -> None:
            attempts["n"] += 1
            self._attempt = attempts["n"]

        async def connect(self) -> None:
            if self._attempt == 1:
                raise RuntimeError("cli spawn failed")

        async def disconnect(self) -> None:
            pass

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            yield _FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FlakyOptions
    fake_sdk.ClaudeSDKClient = FlakyClient
    fake_sdk.ResultMessage = _FakeResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    pool = ClaudeStreamClientPool(max_concurrent=4)
    with caplog.at_level(logging.WARNING):
        pool.precede_connect(session_id="sess-flaky", model="haiku")
        _wait_until(lambda: attempts["n"] == 1)
        _wait_until(lambda: "sess-flaky" not in pool._precede_pending)

    assert any("precede_connect_failed" in rec.getMessage() for rec in caplog.records)
    # The entry is still there, just unconnected -- the pool never surfaces
    # the background failure to a caller that never asked for it.
    assert pool._entries["sess-flaky"]._client is None

    claimed = pool.entry_for(session_id="sess-flaky")
    await _consume(
        claimed,
        payload="p",
        native_blocks=[],
        session_id="sid",
        timeout=5.0,
        on_construct=lambda: None,
        model="haiku",
    )
    assert attempts["n"] == 2  # the real turn's own, successful connect


# --------------------------------------------------------------------------- #
# The pending-precede-connect count is capped.
# --------------------------------------------------------------------------- #
async def test_precede_connect_skips_beyond_the_pending_cap(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """A burst of session-opens must never queue unboundedly for connect
    slots ahead of a real turn -- see the module docstring's rationale.

    SABOTAGE: drop the ``len(pool._precede_pending) >= max_precede_connects()``
    check -> the second precede-connect below also mints an entry -> the
    ``sess-second`` assertion goes red.
    """
    monkeypatch.setattr(csb, "max_precede_connects", lambda: 1)
    release = threading.Event()

    class BlockingOptions:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class BlockingClient:
        def __init__(self, options: BlockingOptions) -> None:
            pass

        async def connect(self) -> None:
            await asyncio.get_running_loop().run_in_executor(None, release.wait)

        async def disconnect(self) -> None:
            pass

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = BlockingOptions
    fake_sdk.ClaudeSDKClient = BlockingClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    pool = ClaudeStreamClientPool(max_concurrent=4)
    try:
        pool.precede_connect(session_id="sess-first", model="haiku")
        _wait_until(lambda: "sess-first" in pool._precede_pending)

        with caplog.at_level(logging.INFO):
            pool.precede_connect(session_id="sess-second", model="haiku")

        # The at-cap session is never even minted an entry.
        assert "sess-second" not in pool._entries
        assert any("precede_connect_skipped" in rec.getMessage() for rec in caplog.records)
    finally:
        release.set()

    _wait_until(lambda: "sess-first" not in pool._precede_pending)

    # The slot is free again once the first pre-connect finishes. Wait for
    # this one to fully settle too (not just be minted) -- ``release`` is
    # already set, so it completes quickly -- otherwise its background thread
    # would leak into the NEXT test and touch a ``claude_agent_sdk`` this
    # test's ``monkeypatch`` has already reverted.
    pool.precede_connect(session_id="sess-third", model="haiku")
    _wait_until(lambda: "sess-third" not in pool._precede_pending)
    assert "sess-third" in pool._entries


# --------------------------------------------------------------------------- #
# Eviction: an unclaimed pre-connected entry follows the SAME idle-TTL rule.
# --------------------------------------------------------------------------- #
def test_idle_reap_evicts_an_unclaimed_precede_connected_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody ever sends a first turn on this session -- the connection must
    not pile up forever; the existing idle-TTL sweep reaps it exactly like a
    claimed session's connection (no separate mechanism needed).
    """

    class _FakeClock:
        def __init__(self, start: float = 0.0) -> None:
            self.now = start

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    clock = _FakeClock()
    monkeypatch.setattr(ccs.time, "monotonic", clock)
    monkeypatch.setattr(csb, "session_idle_ttl_s", lambda: 15.0)

    state = _install_fake_sdk(monkeypatch)
    # Fake clock: pin the entry_for sweep path alone (the timer reaper's own
    # precede-connect pin is in test_claude_code_idle_reaper.py).
    pool = ClaudeStreamClientPool(max_concurrent=4, reap_on_timer=False)

    pool.precede_connect(session_id="sess-abandoned", model="haiku")
    # Wait for the WHOLE background attempt (connect + its own _mark_idle())
    # to finish -- discard from _precede_pending happens strictly after
    # _mark_idle() in the same thread, so this can't race the clock advance.
    _wait_until(lambda: "sess-abandoned" not in pool._precede_pending)
    assert state["connected"] == 1

    clock.advance(20.0)  # past the TTL, nobody ever claimed it

    # Any OTHER session's entry_for triggers the sweep.
    pool.entry_for(session_id="sess-other")

    assert "sess-abandoned" not in pool._entries
