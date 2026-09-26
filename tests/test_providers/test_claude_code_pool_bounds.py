"""Process-wide concurrency cap on the claude_code streaming pool (S2 B1/B2).

The idle-reap (test_claude_code_idle_reap.py) cannot bound a genuinely
ACTIVE fan-out: two sessions truly streaming at the same instant are each
BUSY, never idle, so neither is reap-eligible by design -- reaping a busy
connection would pull it out from under its own caller. The concurrency cap
is the complementary lever: every pooled entry's CONNECT draws from ONE
process-wide semaphore
(:func:`~clio_agent.providers.claude_code_stream_bounds.max_concurrent_claude_processes`)
and releases it on disconnect, so total resident ``claude`` CLI subprocesses
is bounded by N regardless of how many sessions want one. A connect beyond
the cap WAITS for a free slot -- it never fails or degrades a turn, only
queues it.

S2 rekeys the pool by GACT session id (not ``(model, cwd, thinking, scope)``):
these pins are updated for the new :meth:`_StreamClientEntry` constructor
shape (no ``options_factory``; config is resolved per call by
``_ensure_client``) and :meth:`ClaudeStreamClientPool.entry_for`'s new
``session_id=`` signature. B2's session-open precede-connect pins live in
``test_claude_code_precede_connect.py``.

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
            for key, value in kwargs.items():
                setattr(self, key, value)

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
    membership (two DISTINCT session-keyed entries can coexist as objects;
    only one may hold a connected CLI process at a time).

    SABOTAGE: drop the connect-slot acquire from ``_ensure_client`` -> entry_b's
    task finishes on the FIRST check below (it never had to wait) -> red.
    """
    state = _install_fake_sdk(monkeypatch)
    slots = threading.Semaphore(1)
    entry_a = ccs._StreamClientEntry(connect_slots=slots)
    entry_b = ccs._StreamClientEntry(connect_slots=slots)

    await entry_a._ensure_client(lambda: None, model="m")
    assert state["connected"] == 1

    task_b = asyncio.create_task(entry_b._ensure_client(lambda: None, model="m"))
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

    entry_a = ccs._StreamClientEntry(connect_slots=slots)
    with pytest.raises(RuntimeError, match="boom"):
        await entry_a._ensure_client(lambda: None, model="m")

    # The slot must be free again -- prove it with a real connect on entry_b.
    state = _install_fake_sdk(monkeypatch)
    entry_b = ccs._StreamClientEntry(connect_slots=slots)
    await asyncio.wait_for(entry_b._ensure_client(lambda: None, model="m"), timeout=0.3)
    assert state["connected"] == 1


async def test_connect_gate_is_a_noop_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``connect_slots=None`` (the constructor default) never gates -- every
    existing single-entry test relies on this uncapped behaviour."""
    state = _install_fake_sdk(monkeypatch)
    entry = ccs._StreamClientEntry()  # no connect_slots passed
    await asyncio.wait_for(entry._ensure_client(lambda: None, model="m"), timeout=0.3)
    assert state["connected"] == 1


async def test_connect_gate_reclaims_an_idle_sibling_while_queued() -> None:
    """A waiter must re-check idle siblings after the allocation-time sweep.

    This pins the live parent/child race: the parent was busy when the child
    entry was allocated, became idle immediately afterward, and retained the
    only process slot.  Without the waiter callback the acquire below never
    completes even though reclaiming the now-idle parent is safe.
    """
    slots = threading.Semaphore(0)
    reclaimed = threading.Event()

    def reclaim() -> None:
        if not reclaimed.is_set():
            reclaimed.set()
            slots.release()

    entry = ccs._StreamClientEntry(connect_slots=slots, reclaim_idle_slot=reclaim)
    await asyncio.wait_for(entry._acquire_connect_slot(), timeout=0.8)
    assert reclaimed.is_set()


def test_pool_wires_its_connect_slots_into_every_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool's own cap (not the global default) is what every entry it
    mints actually gates on -- ``max_concurrent=`` must reach the entries."""
    pool = ccs.ClaudeStreamClientPool(max_concurrent=1)
    sess_a = pool.entry_for(session_id="sess-a")
    sess_b = pool.entry_for(session_id="sess-b")
    # SABOTAGE: forget to pass connect_slots=self._connect_slots in entry_for's
    # _StreamClientEntry(...) construction -> both entries gate on nothing ->
    # this identity check goes red.
    assert sess_a._connect_slots is pool._connect_slots
    assert sess_b._connect_slots is pool._connect_slots
    assert sess_b._reclaim_idle_slot is not None


def test_entry_for_evicts_a_dead_entry_and_replaces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``_dead`` entry at a session's key is never handed out again -- the
    pool mints a fresh replacement, typed and logged.

    SABOTAGE: return the dead entry as-is from ``entry_for`` -> the caller's
    next connect attempt raises the F6b ``dead_entry_error_message`` forever
    -> this identity check goes red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=4)
    first = pool.entry_for(session_id="sess-dead")
    first._dead = True

    second = pool.entry_for(session_id="sess-dead")
    assert second is not first


def test_reclaim_idle_for_slot_evicts_only_idle_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap-pressure sweep (``_reclaim_idle_for_slot``) reaps idle
    sessions, never a genuinely busy one."""
    pool = ccs.ClaudeStreamClientPool(max_concurrent=1)
    idle = pool.entry_for(session_id="parent")
    busy = pool.entry_for(session_id="sibling")
    idle._mark_idle()
    busy._mark_busy()
    monkeypatch.setattr(ccs, "reap_idle_session_entry", lambda _sid, _entry: None)

    assert pool._reclaim_idle_for_slot() == 1
    assert pool.entry_for(session_id="sibling") is busy
    assert pool.entry_for(session_id="parent") is not idle
