"""The resident set is the one residency owner: ARC scopes count with the ledgers (R6).

ARC reports every scope it caches to :class:`ResidentLedgerSet`; one byte cap, one
session-count cap and one idle TTL bound both copies of a session, an evicted session is
handed to ``on_evict`` (which releases its ARC scopes), and :meth:`peek` reads without
making anything resident.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from clio_agent.arc.segment_cache import reads_are_transient
from clio_agent.gact.resident_ledgers import ResidentLedgerConfig, ResidentLedgerSet
from clio_agent.gact.resident_sweeper import ResidentSweeper, release_free_heap
from clio_agent.gact.types import Message, Part


def _msg(mid: str, sid: str) -> Message:
    return Message(
        id=mid,
        session_id=sid,
        role="user",
        created_at="t",
        updated_at="t",
        parts=[Part(id=f"{mid}_p0", type="text", text="hi")],
    )


class _Store:
    def __init__(self, data: dict[str, list[Message]]) -> None:
        self._data = data
        self.transient_loads = 0

    def load_session(self, sid: str) -> Optional[list[Message]]:
        self.transient_loads += reads_are_transient()
        rows = self._data.get(sid)
        return list(rows) if rows is not None else None

    def session_ids(self) -> list[str]:
        return list(self._data)

    def has_session(self, sid: str) -> bool:
        return sid in self._data


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _set(**kwargs) -> tuple[ResidentLedgerSet, list[str], list[dict], _Clock]:  # type: ignore[no-untyped-def]
    evicted: list[str] = []
    audit: list[dict] = []
    clock = _Clock()
    config = kwargs.pop("config", ResidentLedgerConfig(max_bytes=10**9, idle_ttl_s=100.0))
    rl = ResidentLedgerSet(
        kwargs.pop("store", _Store({})),
        config=config,
        audit=audit.append,
        on_evict=evicted.append,
        clock=clock,
        **kwargs,
    )
    return rl, evicted, audit, clock


def test_arc_scopes_count_against_the_byte_cap() -> None:
    rl, evicted, audit, clock = _set(
        config=ResidentLedgerConfig(max_bytes=1000, max_sessions=100, idle_ttl_s=10**6)
    )
    rl.scope_resident("old", "_events", 600)
    clock.now += 1
    rl.scope_resident("new", "_events", 600)  # 1200 > 1000: the oldest idle one goes
    assert rl.sweep() == 1
    assert evicted == ["old"]
    assert rl.arc_session_ids == ["new"]
    assert rl.arc_bytes == 600
    row = next(a for a in audit if a["reason"] == "capacity_bytes")
    assert row["session_id"] == "old" and row["arc_bytes"] == 600


def test_an_idle_arc_only_session_is_released_by_the_sweep() -> None:
    rl, evicted, audit, clock = _set()
    rl.scope_resident("child", "_events", 10)
    rl.scope_resident("child", "_events/m", 20)
    assert rl.sweep() == 0  # not idle yet
    clock.now += 500
    assert rl.sweep() == 1
    assert evicted == ["child"]
    assert rl.arc_bytes == 0
    assert any(a["reason"] == "idle_ttl" and a["session_id"] == "child" for a in audit)


def test_a_touched_scope_keeps_its_session() -> None:
    rl, evicted, _audit, clock = _set()
    rl.scope_resident("s", "_events", 10)
    clock.now += 90
    rl.scope_touched("s")
    clock.now += 90  # 180 since the load, 90 since the last read
    assert rl.sweep() == 0
    assert evicted == []


def test_an_active_session_keeps_its_arc_scopes() -> None:
    rl, evicted, _audit, clock = _set(is_active=lambda sid: sid == "viewed")
    rl.scope_resident("viewed", "_events", 10)
    clock.now += 10_000
    assert rl.sweep() == 0
    assert evicted == []


def test_ledger_and_arc_scopes_of_one_session_are_one_unit() -> None:
    store = _Store({"s": [_msg("m", "s")], "t": [_msg("m", "t")]})
    rl, evicted, _audit, clock = _set(
        store=store, config=ResidentLedgerConfig(max_bytes=10**9, max_sessions=1, idle_ttl_s=10**6)
    )
    rl.get("s", [])
    rl.scope_resident("s", "_events/m", 50)
    clock.now += 1
    rl.get("t", [])  # two sessions > cap 1: s goes, ledger and ARC together
    assert evicted == ["s"]
    assert rl.resident_session_ids == ["t"]
    assert "s" not in rl.arc_session_ids


def test_released_and_dropped_scopes_leave_the_accounting() -> None:
    rl, _evicted, _audit, _clock = _set()
    rl.scope_resident("s", "a", 10)
    rl.scope_resident("s", "b", 20)
    rl.scope_resident("s", "b", 30)  # a re-commit replaces the scope's size
    assert rl.arc_bytes == 40
    rl.scope_dropped("s", "a")
    assert rl.arc_bytes == 30
    rl.session_released("s")
    assert rl.arc_bytes == 0 and rl.arc_session_ids == []


def test_over_budget_asks_for_a_sweep() -> None:
    asked: list[int] = []
    rl, _evicted, _audit, _clock = _set(
        config=ResidentLedgerConfig(max_bytes=100, max_sessions=10, idle_ttl_s=10**6)
    )
    rl.on_over_budget = lambda: asked.append(1)
    rl.scope_resident("s", "a", 50)
    assert asked == []
    rl.scope_resident("s", "b", 80)
    assert asked == [1]


def test_peek_reads_without_making_the_session_resident() -> None:
    store = _Store({"s": [_msg("m", "s")]})
    rl, _evicted, audit, _clock = _set(store=store)
    assert [m.id for m in rl.peek("s") or []] == ["m"]
    assert rl.resident_count == 0
    assert store.transient_loads == 1  # read inside transient_reads: ARC caches nothing
    assert not any(a["reason"] == "rehydrate" for a in audit)
    assert rl.peek("missing") is None
    rl.get("s", [])
    assert rl.peek("s") is rl.get_if_resident("s")  # a resident ledger is served as is


def test_the_sweeper_sweeps_on_request_and_on_its_interval() -> None:
    calls: list[int] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        sweeper = ResidentSweeper(lambda: calls.append(1) or 0, interval_s=0.05, loop=lambda: loop)
        sweeper.request()
        await asyncio.sleep(0.2)
        assert sweeper._task is not None
        sweeper._task.cancel()

    asyncio.run(scenario())
    assert len(calls) >= 2  # the requested sweep, then at least one periodic one


def test_the_sweeper_does_nothing_before_the_loop_runs() -> None:
    sweeper = ResidentSweeper(lambda: 0, interval_s=1.0, loop=lambda: None)
    sweeper.request()
    assert sweeper._task is None


def test_release_free_heap_reports_whether_it_could_trim() -> None:
    assert release_free_heap() in {True, False}
