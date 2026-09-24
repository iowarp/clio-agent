"""The claude_code connection pools stay bounded now that they key per effort level."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from clio_agent.providers import claude_code_sdk_pool
from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool
from clio_agent.providers.claude_code_stream_bounds import sweep_stream_entries


class _FakeSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def test_sdk_pool_evicts_only_idle_sessions_and_overflows_when_all_busy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_BASE_CONNECTIONS", "2")
    monkeypatch.setattr(claude_code_sdk_pool, "_SdkSession", _FakeSession)
    caplog.set_level(logging.INFO, logger="clio_agent.providers.claude_code_sdk_pool")
    pool = claude_code_sdk_pool._SdkSessionPool()

    low = pool._session_for("opus", None, "effort:low")
    high = pool._session_for("opus", None, "effort:high")
    low._lock.acquire()  # a completion is in flight on both
    high._lock.acquire()
    started = time.monotonic()
    pool._session_for("opus", None, "effort:max")
    assert time.monotonic() - started < 1.0  # never waits on a busy session
    assert len(pool._sessions) == 3  # temporary overflow, nothing busy evicted
    assert not low.closed.is_set() and not high.closed.is_set()
    assert any("claude_code_pool_over_cap" in r.getMessage() for r in caplog.records)

    low._lock.release()  # low goes idle: the next lookup evicts it
    pool._session_for("opus", None, "effort:max")
    assert low.closed.wait(2.0)
    assert ("opus", None, "effort:low") not in pool._sessions
    assert len(pool._sessions) == 2
    assert any("claude_code_sdk_session_evicted" in r.getMessage() for r in caplog.records)
    high._lock.release()


def test_stream_pool_returns_the_same_entry_for_an_existing_key_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_BASE_CONNECTIONS", "4")
    pool = ClaudeStreamClientPool(max_concurrent=4)
    entries = [
        pool.entry_for(model="opus", cwd=None, thinking={"effort": level})
        for level in ("low", "medium", "high", "max")
    ]

    again = pool.entry_for(model="opus", cwd=None, thinking={"effort": "low"})

    assert again is entries[0]
    assert len(pool._entries) == 4
    assert not any(entry._dead for entry in entries)


class _FakeEntry:
    def __init__(self, idle: float | None) -> None:
        self._idle = idle
        self._dead = False

    def idle_for(self) -> float | None:
        return self._idle


class _FakePool:
    def __init__(self, entries: dict[tuple[str, Any, Any, str], _FakeEntry]) -> None:
        self._guard = threading.Lock()
        self._entries = entries


def test_stream_pool_evicts_idle_base_entries_for_a_new_key_and_marks_them_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_BASE_CONNECTIONS", "2")
    busy = _FakeEntry(None)
    entries = {
        ("opus", None, "a", ""): _FakeEntry(50.0),
        ("opus", None, "b", ""): _FakeEntry(5.0),
        ("opus", None, "c", ""): busy,
        ("opus", None, "d", "scope-1"): _FakeEntry(1.0),
    }
    pool = _FakePool(entries)

    evicted = sweep_stream_entries(pool, False, ("opus", None, "new", ""))  # type: ignore[arg-type]

    # Room for the NEW base entry under a cap of 2: the two idle ones go,
    # longest-idle first; the in-flight entry and scoped entries are untouched.
    assert [key[2] for key, _ in evicted] == ["a", "b"]
    assert all(entry._dead for _, entry in evicted)
    assert ("opus", None, "c", "") in pool._entries
    assert ("opus", None, "d", "scope-1") in pool._entries


def test_stream_pool_never_evicts_the_requested_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_CLAUDE_CODE_MAX_BASE_CONNECTIONS", "1")
    requested = ("opus", None, "a", "")
    pool = _FakePool({requested: _FakeEntry(500.0), ("opus", None, "b", ""): _FakeEntry(1.0)})

    evicted = sweep_stream_entries(pool, False, requested)  # type: ignore[arg-type]

    assert [key for key, _ in evicted] == [("opus", None, "b", "")]
    assert requested in pool._entries
