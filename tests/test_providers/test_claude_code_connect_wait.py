"""#1305: the claude_code process-wide connect-slot QUEUE WAIT is surfaced,
typed, and counted as turn progress -- and never burns the SDK bridge's
per-call timeout while genuinely still queued.

Root cause (owner root-cause comment, #1305): the process-wide connect gate
(``max_concurrent_claude_processes``) makes a queued connect WAIT for a free
slot ("never fails/degrades" -- the documented contract). Pre-fix, that wait
ran INSIDE the same ``asyncio.timeout(timeout)`` region as the actual SDK
query/receive exchange, so a slot wait alone exceeding the per-call timeout
(600s live) raised ``TimeoutError`` while the connect was genuinely still
queued, not stalled -- and, invisibly to the trace, the queue itself fed
nothing the turn's 900s no-progress watchdog reads, so a long-but-healthy
queue could ALSO be killed as "no progress".

This module pins the fix at the lowest level that can prove it without a real
``claude`` CLI or network: :func:`claude_code_stream_bounds.await_connect_slot`
(the owner-module wait loop) and :meth:`_StreamClientEntry._ensure_client`'s
restructured timeout accounting. Each pin carries an inline SABOTAGE note.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from types import ModuleType
from typing import Any

import pytest

from clio_agent.providers import claude_code_sessions as ccs
from clio_agent.providers import claude_code_stream_bounds as csb
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from clio_agent.runtime import lm_activity


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    """Every test starts and ends with an empty streaming client pool."""
    _reset_sessions_for_tests()
    yield
    _reset_sessions_for_tests()


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A minimal fake ``claude_agent_sdk`` -- connect/query/receive, no real I/O."""
    state: dict[str, Any] = {"connected": 0, "queried": []}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeResultMessage:
        pass

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            state["queried"].append((prompt, session_id))

        async def receive_response(self) -> Any:
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


async def _drain(entry: ccs._StreamClientEntry, **kwargs: Any) -> list[Any]:
    out: list[Any] = []
    async for msg in entry.stream(**kwargs):
        out.append(msg)
    return out


# --------------------------------------------------------------------------- #
# (a) Typed wait surfacing.
# --------------------------------------------------------------------------- #
async def test_await_connect_slot_emits_typed_surfacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A queued connect emits a typed ``provider.connect_wait`` row naming what
    it waits on, its attempt number, and elapsed time -- no silent waiting.

    SABOTAGE: drop the ``stream_audit(...)`` call from the poll loop -> no
    rows are ever recorded -> this goes red.
    """
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )
    slots = threading.Semaphore(0)  # exhausted: every poll attempt fails
    task = asyncio.create_task(
        csb.await_connect_slot(slots, session_id="sess-surface", poll_interval_s=0.01)
    )
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    connect_rows = [r for r in rows if r["event"] == "provider.connect_wait"]
    assert connect_rows  # SABOTAGE: skip stream_audit -> empty -> red
    first = connect_rows[0]
    assert first["reason"] == "connect_slot_queued"
    assert first["category"] == "session_connect_wait"
    assert first["provider"] == "claude_code_sdk"
    assert first["session_id"] == "sess-surface"
    assert first["waiting_on"] == "claude connect slot"
    assert first["attempt"] == 1  # SABOTAGE: don't increment attempt -> stays 0 -> red
    assert first["elapsed_s"] >= 0.0
    assert "next_retry_s" in first


async def test_await_connect_slot_surfacing_cadence_expands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8: pin the surfacing CADENCE itself -- at least 2 rows, with the gap
    between successive rows GROWING (mirrors arc.rpc_liveness's per-attempt
    backoff), never a flat every-poll-attempt spam.

    SABOTAGE: drop the ``elapsed >= next_surface_at`` gate (surface on every
    failed poll attempt instead) -> dozens of rows with a ~poll_interval_s
    flat gap, not a growing one -> the ``gaps == sorted(gaps)`` /
    non-degenerate-growth assertions go red.
    """
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )
    # Shrink the cadence constants (not the poll interval) so the test proves
    # the SHAPE of the backoff in well under a second of real wall-clock time.
    monkeypatch.setattr(csb, "_SURFACE_INITIAL_S", 0.02)
    monkeypatch.setattr(csb, "_SURFACE_MAX_S", 1.0)
    monkeypatch.setattr(csb, "_SURFACE_BACKOFF_FACTOR", 3.0)

    slots = threading.Semaphore(0)
    task = asyncio.create_task(
        csb.await_connect_slot(slots, session_id="sess-cadence", poll_interval_s=0.01)
    )
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    connect_rows = [r for r in rows if r["event"] == "provider.connect_wait"]
    assert len(connect_rows) >= 2  # SABOTAGE: surface once and never again -> red
    elapsed = [r["elapsed_s"] for r in connect_rows]
    gaps = [b - a for a, b in zip(elapsed, elapsed[1:], strict=False)]
    assert all(g > 0 for g in gaps)  # strictly forward progress between rows
    assert gaps == sorted(gaps)  # the gap between rows never SHRINKS (backoff grows)
    assert gaps[-1] > gaps[0]  # and it genuinely widened, not a flat cadence


# --------------------------------------------------------------------------- #
# (c) Progress-feed: the queue counts as turn-watchdog liveness.
# --------------------------------------------------------------------------- #
async def test_await_connect_slot_feeds_lm_activity_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued connect refreshes the waiting session's LM-activity bucket
    (the SAME record the 900s no-progress watchdog reads via
    ``lm_call_in_flight``) -- a queued turn is progress, never a stall.

    F5 (#1305 review round): the refresh must land on the DISTINCT
    ``queued_last`` field, never ``last`` -- writing ``last`` would silently
    flip the bucket into STREAMING regime (a much tighter ceiling) despite
    zero tokens ever having flowed.

    SABOTAGE: drop the ``note_lm_activity_for(session_id)`` call from the poll
    loop -> ``queued_last`` never advances past the seeded 0.0 -> this goes red.
    """
    sid = "sess-liveness"
    # Seed an existing in-flight bucket, mirroring note_lm_start() having
    # already fired (before the transport call began) for a real LM call.
    lm_activity._STATE[sid] = {"inflight": 1.0, "started": 0.0, "last": 0.0, "queued_last": 0.0}
    try:
        slots = threading.Semaphore(0)
        task = asyncio.create_task(
            csb.await_connect_slot(slots, session_id=sid, poll_interval_s=0.01)
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert lm_activity._STATE[sid]["queued_last"] > 0.0
        assert lm_activity._STATE[sid]["last"] == 0.0  # never touched -- still NON-STREAMING
        assert lm_activity.lm_call_in_flight(sid) is True
    finally:
        lm_activity._STATE.pop(sid, None)


def test_note_lm_activity_for_is_a_true_noop_without_an_existing_bucket() -> None:
    """No fabricated progress: a session with no registered LM call in flight
    stays absent from ``_STATE`` -- never silently vivified as "in flight"."""
    sid = "sess-no-bucket"
    assert sid not in lm_activity._STATE
    lm_activity.note_lm_activity_for(sid)
    # SABOTAGE: auto-vivify via _bucket() like note_lm_activity() does -> a
    # stray inflight=0 bucket appears -> this goes red.
    assert sid not in lm_activity._STATE


# --------------------------------------------------------------------------- #
# (b) The 600s (here: tiny, injected) per-call SDK timeout must bound the
# actual exchange, never the queue wait.
# --------------------------------------------------------------------------- #
async def test_queued_connect_does_not_burn_the_per_call_sdk_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued connect must not raise ``TimeoutError`` purely from waiting
    for a slot, even when the per-call SDK timeout is far shorter than how
    long the wait lasts -- the timeout must cover only the actual SDK
    exchange (construct+connect, then query+receive), never the queue.

    SABOTAGE: put the ``_ensure_client`` call back INSIDE ``_pump``'s
    ``async with asyncio.timeout(timeout):`` block (the pre-#1305 shape) ->
    entry_b's task raises ``TimeoutError`` well before the slot ever frees ->
    this goes red.
    """
    state = _install_fake_sdk(monkeypatch)
    slots = threading.Semaphore(1)
    entry_a = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)
    entry_b = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)

    await entry_a._ensure_client(lambda: None)  # holds the only slot
    assert state["connected"] == 1

    # entry_b's per-call SDK timeout is FAR shorter than how long we keep it
    # queued below -- pre-#1305 this alone raised TimeoutError while entry_b
    # was genuinely still waiting for a slot, never having reached the SDK.
    task_b = asyncio.create_task(
        _drain(entry_b, payload="p", session_id="sid-b", timeout=0.05, on_construct=lambda: None)
    )
    try:
        await asyncio.sleep(0.4)  # >> the 0.05s "call timeout"
        # F8: the prior form (``assert X is None if done else True``) parses as
        # ``assert (X is None) if done else True`` -- since done is False here
        # it silently reduces to ``assert True``, a vacuous no-op. Assert the
        # real property: the task is still genuinely pending (no result AND no
        # exception set), not merely "not done" by some other stalled path.
        assert not task_b.done()  # still queued, not failed/timed out
        assert not task_b.cancelled()

        await entry_a._areset_client()  # free the slot
        result = await asyncio.wait_for(task_b, timeout=1.0)
    finally:
        if not task_b.done():
            task_b.cancel()
    assert result  # entry_b's own (fresh, un-starved) SDK exchange completed
    assert state["connected"] == 2


# --------------------------------------------------------------------------- #
# F4 (#1305 review round): a caller that abandons a STILL-QUEUED stream must
# never leave a "zombie" pump behind -- one that eventually acquires the slot
# and spawns a CLI subprocess nobody is listening to.
# --------------------------------------------------------------------------- #
async def test_abandoning_a_queued_stream_never_consumes_a_slot_or_spawns_a_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that abandons ``stream()`` WHILE still queued for a connect
    slot (before ``register_sdk_stream`` ever ran -- kill-on-cancel has
    nothing to grab onto yet) must not leave ``_pump()`` running: no slot may
    ever be consumed, no CLI subprocess ever spawned, even once the slot
    later frees up.

    SABOTAGE: drop the ``fut.cancel()`` call from ``stream()``'s abandon path
    -> ``_pump()`` keeps running queued, eventually acquires entry_a's freed
    slot and connects -> ``state["connected"]`` goes to 2 -> this goes red.
    """
    state = _install_fake_sdk(monkeypatch)
    slots = threading.Semaphore(1)
    entry_a = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)
    entry_b = ccs._StreamClientEntry(lambda: object(), connect_slots=slots)

    await entry_a._ensure_client(lambda: None)  # holds the only slot
    assert state["connected"] == 1

    gen = entry_b.stream(payload="p", session_id="sid-b", timeout=None, on_construct=lambda: None)
    anext_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.1)  # let entry_b genuinely start queueing
    assert not anext_task.done()

    # The caller abandons: cancel its own await (the async-generator-driving
    # task), never having drained anything.
    anext_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await anext_task
    with contextlib.suppress(Exception):
        await gen.aclose()

    # NOW free entry_a's slot. A zombie entry_b pump would silently consume
    # it and connect a CLI nobody is listening to.
    await entry_a._areset_client()
    await asyncio.sleep(0.3)
    assert state["connected"] == 1  # only entry_a ever connected


# --------------------------------------------------------------------------- #
# Regression pin: a PROMPT (uncontended) slot acquire behaves exactly as
# today -- zero new surfacing/progress overhead on the common fast path.
# --------------------------------------------------------------------------- #
async def test_prompt_slot_acquire_emits_no_new_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: surface/feed liveness even when the FIRST acquire attempt
    succeeds (no queueing at all) -> a row appears -> this goes red."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )
    slots = threading.Semaphore(1)  # immediately available
    await csb.await_connect_slot(slots, session_id="sess-fast")
    assert rows == []


async def test_connect_gate_still_a_noop_when_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``connect_slots=None`` never gates or surfaces -- unchanged by #1305."""
    state = _install_fake_sdk(monkeypatch)
    entry = ccs._StreamClientEntry(lambda: object())  # no connect_slots
    await asyncio.wait_for(
        entry._ensure_client(lambda: None, gact_session_id="sess-z"), timeout=0.3
    )
    assert state["connected"] == 1


# --------------------------------------------------------------------------- #
# Cancellation remains honored (unchanged bounded-poll mechanism).
# --------------------------------------------------------------------------- #
async def test_await_connect_slot_cancellation_leaves_no_orphaned_waiter() -> None:
    """A caller cancelled while queued abandons cleanly -- no phantom
    semaphore waiter silently consumes a later release."""
    slots = threading.Semaphore(0)
    task = asyncio.create_task(
        csb.await_connect_slot(slots, session_id="sess-cancel", poll_interval_s=0.01)
    )
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # A later release must be cleanly acquirable -- no orphaned waiter ate it.
    slots.release()
    assert slots.acquire(False) is True
