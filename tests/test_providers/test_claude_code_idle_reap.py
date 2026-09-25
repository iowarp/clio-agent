"""Idle-reap for the claude_code streaming pool (S2: session-id keyed, B1).

Root cause (pre-S2, e47285fb #COPPER12): a react-loop scope's own isolated
pooled connection could sit resident for the rest of the scope's lifetime
even once quiet. S2 replaced scope-keying with GACT-session-id keying (every
entry is now "session-keyed" — there is no separately-protected "shared base
entry" anymore), and B1 means a session's connection legitimately persists
across many turns — so idle reap now applies uniformly to every entry, gated
purely on real inactivity (:meth:`_StreamClientEntry.idle_for`), never a
"which kind of entry is this" distinction.

This module pins: an idle entry is reclaimed the next time ANY session wants
a connection, a busy (in-flight) entry is never touched, and reaping always
tells the claude_code stateful-delta registry (if an ENGAGED scope was riding
the connection) so the next send on that scope is forced to a full resend
rather than shipping a delta tail to a fresh subprocess with no memory of the
dropped prefix (the correctness guarantee #COPPER12 exists to protect — now
derived from the entry's own ``_last_scope`` bookkeeping instead of a
separate scope<->pool-key registry).

Each pin carries an inline SABOTAGE note.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.providers import claude_code_sessions as ccs
from clio_agent.providers import claude_code_stateful as cst
from clio_agent.providers import claude_code_stream_bounds as csb
from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool


class _FakeClock:
    """A controllable monotonic clock -- deterministic idle-TTL tests, no real sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Every test starts and ends with a clean claude_code stateful registry."""
    cst.stateful_registry().reset_for_tests()
    yield
    cst.stateful_registry().reset_for_tests()


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(ccs.time, "monotonic", clock)
    return clock


# --------------------------------------------------------------------------- #
# _StreamClientEntry.idle_for() semantics.
# --------------------------------------------------------------------------- #
def test_idle_for_none_while_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-stream entry must never report itself reapable."""
    _install_clock(monkeypatch)
    entry = ccs._StreamClientEntry()
    entry._mark_busy()
    # SABOTAGE: return elapsed time regardless of _in_flight -> a live-in-use
    # entry becomes reapable out from under its own caller -> red.
    assert entry.idle_for() is None


def test_idle_for_counts_from_last_completed_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    entry = ccs._StreamClientEntry()
    entry._mark_busy()
    clock.advance(5.0)
    entry._mark_idle()
    clock.advance(3.0)
    assert entry.idle_for() == pytest.approx(3.0)


def test_idle_for_counts_from_construction_when_never_streamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that opens a connection then stalls before its first send is
    still reapable -- idle-since-construction, not idle-since-never."""
    clock = _install_clock(monkeypatch)
    entry = ccs._StreamClientEntry()
    clock.advance(20.0)
    assert entry.idle_for() == pytest.approx(20.0)


# --------------------------------------------------------------------------- #
# Pool-level sweep: busy entry protected, an idle entry reaped only once past TTL.
# --------------------------------------------------------------------------- #
def test_sweep_never_touches_a_busy_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()
    entry = pool.entry_for(session_id="sess-a")
    entry._mark_busy()
    clock.advance(1000.0)
    evicted = ccs.sweep_idle_session_entries(pool, ttl_s=1.0)
    assert evicted == []  # a live-in-use connection must never be pulled from under its caller


def test_sweep_reaps_an_idle_entry_past_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()
    entry = pool.entry_for(session_id="sess-a")
    entry._mark_idle()
    clock.advance(20.0)
    evicted = ccs.sweep_idle_session_entries(pool, ttl_s=15.0)
    assert [session_id for session_id, _ in evicted] == ["sess-a"]
    assert entry._dead is True
    # Genuinely gone from the pool -- the next entry_for mints a fresh one.
    fresh = pool.entry_for(session_id="sess-a")
    assert fresh is not entry


def test_sweep_leaves_a_not_yet_idle_entry_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()
    entry = pool.entry_for(session_id="sess-a")
    entry._mark_idle()
    clock.advance(5.0)  # under the 15s TTL
    evicted = ccs.sweep_idle_session_entries(pool, ttl_s=15.0)
    assert evicted == []
    assert pool.entry_for(session_id="sess-a") is entry


# --------------------------------------------------------------------------- #
# entry_for() wiring: a NEW session's own request is what triggers the sweep --
# the moment peak is about to grow is exactly when a quiet sibling should be
# reclaimed first.
# --------------------------------------------------------------------------- #
def test_entry_for_new_session_reaps_an_idle_sibling_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent session that has gone idle must not keep inflating peak once a
    sibling session needs a connection.

    SABOTAGE: drop the sweep call from entry_for -> the idle entry survives
    past its TTL and this assertion goes red.
    """
    monkeypatch.setattr(csb, "session_idle_ttl_s", lambda: 15.0)
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()
    parent_entry = pool.entry_for(session_id="parent")
    parent_entry._mark_idle()
    clock.advance(16.0)  # past the (pinned) 15s TTL

    child_entry = pool.entry_for(session_id="child")

    assert child_entry is not parent_entry
    # The parent's OWN entry was reaped as a side effect of the child's allocation.
    reaped_again = pool.entry_for(session_id="parent")
    assert reaped_again is not parent_entry


def test_entry_for_leaves_a_fresh_sibling_session_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two genuinely-concurrent, actively-used sessions must NOT be swept just
    because a third one is being minted -- the whole point is preserving
    legitimate concurrency, not capping it."""
    monkeypatch.setattr(csb, "session_idle_ttl_s", lambda: 15.0)
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()
    data_entry = pool.entry_for(session_id="data")
    data_entry._mark_busy()  # still actively streaming
    clock.advance(16.0)

    analysis_entry = pool.entry_for(session_id="analysis")

    assert analysis_entry is not data_entry
    assert pool.entry_for(session_id="data") is data_entry


def test_session_idle_ttl_s_reads_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S`` overrides the 15s default."""
    monkeypatch.setenv("CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S", "42")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        assert csb.session_idle_ttl_s() == pytest.approx(42.0)
    finally:
        conf.reload()


# --------------------------------------------------------------------------- #
# Correctness (the load-bearing pin): reaping an idle, delta-ENGAGED session
# must force its NEXT send to a full resend, never a silent delta onto a
# subprocess with no memory of the dropped prefix.
# --------------------------------------------------------------------------- #
def test_reap_forces_the_next_stateful_send_to_a_full_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle-reap must tell the claude_code stateful registry, or the next
    call on this scope would classify as a delta (append-only messages) and
    ship only the tail to a brand-new subprocess that never saw the prefix --
    a silent conversation-coherence bug, not merely a slower reconnect.

    SABOTAGE: drop the ``stateful_registry().note_provider_error(...)`` call in
    ``reap_idle_session_entry`` -> the registry still thinks its last-seen
    prefix is live, the next ``plan()`` call classifies as delta, and this
    goes red.
    """
    monkeypatch.setattr(csb, "session_idle_ttl_s", lambda: 15.0)
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: False)  # audit is a side channel here
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()

    scope = "loop-a"
    session_key = (scope, "haiku", "/w", None)
    registry = cst.stateful_registry()
    # Prime a live session: call 1 (full/first_call).
    registry.plan(
        session_key=session_key, scope_token=scope, messages=[{"role": "user", "content": "a"}]
    )

    entry = pool.entry_for(session_id="gact-sess-1")
    # Simulate the entry having last carried this engaged scope's send, at the
    # tracked (model, cwd, thinking) the registry keyed on.
    entry._model, entry._cwd, entry._thinking_key, entry._last_scope = "haiku", "/w", None, scope
    entry._mark_idle()
    clock.advance(20.0)

    # A SECOND session's request is what actually triggers the sweep.
    pool.entry_for(session_id="gact-sess-2")

    # An append-only extension that WOULD be a delta (call 2 normally is) is
    # instead forced full=provider_error because the connection was reaped.
    plan, _handle = registry.plan(
        session_key=session_key,
        scope_token=scope,
        messages=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
    )
    assert plan.mode == "full"
    assert plan.reason == "provider_error"


def test_reap_emits_a_typed_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-silent-fallback (#775): the reap is queryable structured data, not a
    silent resource drop."""
    monkeypatch.setattr(csb, "session_idle_ttl_s", lambda: 15.0)
    clock = _install_clock(monkeypatch)
    pool = ClaudeStreamClientPool()

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )

    pool.entry_for(session_id="parent")
    clock.advance(20.0)
    pool.entry_for(session_id="child")

    reaped_rows = [r for r in rows if r.get("reason") == "idle_reaped"]
    assert reaped_rows  # SABOTAGE: skip the stream_audit call on reap -> red
    assert reaped_rows[0]["category"] == "session_idle_reap"
    assert reaped_rows[0]["provider"] == "claude_code_sdk"
