"""The claude_code connection pools stay bounded now that they key per effort level."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from clio_agent.providers import claude_code_sdk_pool
from clio_agent.providers.claude_code_stream_bounds import sweep_stream_entries


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_sdk_session_pool_evicts_least_recently_used(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(claude_code_sdk_pool, "_SdkSession", _FakeSession)
    pool = claude_code_sdk_pool._SdkSessionPool()
    caplog.set_level(logging.INFO, logger="clio_agent.providers.claude_code_sdk_pool")

    first = pool._session_for("opus", None, "effort:low")
    for level in ("medium", "high", "xhigh"):
        pool._session_for("opus", None, f"effort:{level}")
    assert pool._session_for("opus", None, "effort:low") is first  # refreshed as most recent
    pool._session_for("opus", None, "effort:max")

    assert len(pool._sessions) == pool.MAX_SESSIONS
    assert first.closed is False
    assert ("opus", None, "effort:medium") not in pool._sessions
    assert any("claude_code_sdk_session_evicted" in r.getMessage() for r in caplog.records)


class _FakeEntry:
    def __init__(self, idle: float | None) -> None:
        self._idle = idle

    def idle_for(self) -> float | None:
        return self._idle


class _FakePool:
    def __init__(self, entries: dict[tuple[str, Any, Any, str], _FakeEntry]) -> None:
        import threading

        self._guard = threading.Lock()
        self._entries = entries


def test_stream_pool_evicts_idle_base_entries_beyond_the_cap(
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

    evicted = sweep_stream_entries(pool, scoped=False)  # type: ignore[arg-type]

    # Room for one new base entry under a cap of 2: the two idle ones go,
    # longest-idle first; the in-flight entry and scoped entries are untouched.
    assert [key[2] for key, _ in evicted] == ["a", "b"]
    assert ("opus", None, "c", "") in pool._entries
    assert ("opus", None, "d", "scope-1") in pool._entries
