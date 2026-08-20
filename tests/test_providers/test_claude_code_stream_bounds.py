"""Process-wide concurrency cap on the claude_code streaming pool
(release-gating memory regression, mcp_mem_attribution.py peak/final).

The idle-reap (test_claude_code_idle_reap.py) cannot bound a genuinely
ACTIVE fan-out: two experts truly streaming at the same instant (e.g. a
``spawn_agents_parallel`` fan-out) are each BUSY, never idle, so neither is
reap-eligible by design -- reaping a busy connection would pull it out from
under its own caller. The concurrency cap is the complementary lever: every
pooled entry's CONNECT draws from ONE process-wide semaphore
(:func:`~clio_agent.providers.claude_code_stream_bounds.max_concurrent_claude_processes`)
and releases it on disconnect, so total resident ``claude`` CLI subprocesses
is bounded by N regardless of how many concurrent scopes want one. A connect
beyond the cap WAITS for a free slot -- it never fails or degrades a turn,
only queues it.

Each pin carries an inline SABOTAGE note.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType
from typing import Any

import pytest

from clio_agent.providers import claude_code_sessions as ccs


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A minimal fake ``claude_agent_sdk`` -- connect/disconnect only (no
    streaming needed for these connect-gate pins)."""
    state: dict[str, Any] = {"connected": 0, "disconnected": 0}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            state["disconnected"] += 1

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


# --------------------------------------------------------------------------- #
# Config resolution.
# --------------------------------------------------------------------------- #
def test_max_concurrent_claude_processes_reads_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES", "7")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        assert ccs.max_concurrent_claude_processes() == 7
    finally:
        conf.reload()


def test_max_concurrent_claude_processes_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured 0/negative cap must never deadlock every connect -- clamp to 1."""
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES", "0")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        # SABOTAGE: return the raw (possibly 0) resolved value -> a 0-slot
        # semaphore never admits a single connect -> every turn hangs -> red.
        assert ccs.max_concurrent_claude_processes() == 1
    finally:
        conf.reload()


# --------------------------------------------------------------------------- #
# The connect gate itself: bounds CONCURRENTLY-CONNECTED subprocesses, not
# merely how many pool entries exist.
# --------------------------------------------------------------------------- #
async def test_connect_gate_queues_a_connect_beyond_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a 1-slot cap, a second entry's connect must WAIT for the first
    entry's disconnect -- proving the cap bounds live subprocesses, not pool
    membership (two DISTINCT scope-keyed entries can coexist as objects; only
    one may hold a connected CLI process at a time).

    SABOTAGE: drop the connect-slot acquire from ``_ensure_client`` -> entry_b's
    task finishes on the FIRST check below (it never had to wait) -> red.

    Never cancels the queued task (real usage almost never does either, and
    a genuinely cancelled bounded-poll acquire is covered on its own terms
    by the acquire's own bounded-poll design, not simulated here) — instead
    starts it as a background task, proves it is STILL PENDING while the cap
    is held, then releases and lets it run to natural completion.
    """
    state = _install_fake_sdk(monkeypatch)
    slots = threading.Semaphore(1)
    entry_a = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)
    entry_b = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)

    await entry_a._ensure_client(lambda: None)
    assert state["connected"] == 1

    task_b = asyncio.create_task(entry_b._ensure_client(lambda: None))
    try:
        await asyncio.sleep(0.3)
        assert not task_b.done()  # entry_b is queued, not connected
        assert state["connected"] == 1  # entry_b never got its slot

        # Releasing entry_a's slot unblocks entry_b.
        await entry_a._areset_client()
        await asyncio.wait_for(task_b, timeout=1.0)
    finally:
        if not task_b.done():
            task_b.cancel()
    assert state["connected"] == 2


async def test_connect_gate_releases_the_slot_on_a_failed_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that raises must still release its slot -- otherwise one
    failed connect permanently steals a slot from every future turn.

    SABOTAGE: drop the ``except BaseException: ... release() ... raise`` arm
    in ``_ensure_client`` -> the slot leaks -> the second (successful) entry
    below hangs -> red.
    """
    slots = threading.Semaphore(1)

    class BoomOptions:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class BoomClient:
        def __init__(self, options: BoomOptions) -> None:
            pass

        async def connect(self) -> None:
            raise RuntimeError("boom")

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = BoomOptions
    fake_sdk.ClaudeSDKClient = BoomClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    entry_a = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)
    with pytest.raises(RuntimeError, match="boom"):
        await entry_a._ensure_client(lambda: None)

    # The slot must be free again -- prove it with a real connect on entry_b.
    state = _install_fake_sdk(monkeypatch)
    entry_b = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)
    await asyncio.wait_for(entry_b._ensure_client(lambda: None), timeout=0.3)
    assert state["connected"] == 1


async def test_connect_gate_is_a_noop_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``connect_slots=None`` (the constructor default) never gates -- every
    existing single-entry test relies on this uncapped behaviour."""
    state = _install_fake_sdk(monkeypatch)
    entry = ccs._StreamClientEntry(lambda: object())  # no connect_slots passed
    await asyncio.wait_for(entry._ensure_client(lambda: None), timeout=0.3)
    assert state["connected"] == 1


def test_pool_wires_its_connect_slots_into_every_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool's own cap (not the global default) is what every entry it
    mints actually gates on -- ``max_concurrent=`` must reach the entries."""
    pool = ccs.ClaudeStreamClientPool(max_concurrent=1)
    base = pool.entry_for(model="m", cwd="/w", thinking=None)
    scoped = pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a")
    # SABOTAGE: forget to pass connect_slots=self._connect_slots in entry_for's
    # _StreamClientEntry(...) construction -> both entries gate on nothing ->
    # this identity check goes red.
    assert base._connect_slots is pool._connect_slots
    assert scoped._connect_slots is pool._connect_slots
