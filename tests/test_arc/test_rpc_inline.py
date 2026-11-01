"""GIL-free async store RPCs run on the caller thread; stalls still end typed.

A pooled stall-watch worker exists because a blocking native call holds the GIL and
would stop the watcher from waking. An async put or delete returns a future at once
and ``await_future`` watches the daemon's progress itself, so it runs inline, without
the thread hand-off and the progress sample that cost about 1.2 ms per RPC.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import rpc_liveness
from clio_agent.arc.clio_core_async_ops import ClioCoreFutureTimeout
from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError


class _Store:
    def __init__(self, cached: bool) -> None:
        self.reconnects = 0
        self.stalled: list[str] = []
        self._gate = SimpleNamespace(port=1, note_rpc_stalled=self.stalled.append)
        self._tag_ids = SimpleNamespace(cached=lambda kind: cached)

    def _live(self) -> None:
        return None

    def _reconnect(self) -> None:
        self.reconnects += 1


@pytest.fixture(autouse=True)
def _no_sampling(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any daemon progress sample; the inline path must take none."""
    from clio_agent.arc import daemon_progress

    samples: list[str] = []

    def sample(*args: Any, **kwargs: Any) -> Any:
        samples.append("sample")
        return 0.0

    monkeypatch.setattr(daemon_progress, "daemon_work", sample)
    return samples


def test_a_cached_tag_runs_the_rpc_on_the_caller_thread(_no_sampling: list[str]) -> None:
    store = _Store(cached=True)
    seen = rpc_liveness.guarded_store_rpc(
        store, "put", lambda: threading.current_thread(), inline_kind="segments"
    )
    assert seen is threading.current_thread()
    assert _no_sampling == []


def test_an_uncached_tag_uses_the_stall_watch_worker() -> None:
    store = _Store(cached=False)
    seen = rpc_liveness.guarded_store_rpc(
        store, "put", lambda: threading.current_thread(), inline_kind="segments"
    )
    assert seen is not threading.current_thread()


def test_an_inline_rpc_error_is_raised_unchanged() -> None:
    store = _Store(cached=True)

    def refuse() -> None:
        raise RuntimeError("PutBlob refused")

    with pytest.raises(RuntimeError, match="PutBlob refused"):
        rpc_liveness.guarded_store_rpc(store, "put", refuse, inline_kind="segments")
    assert store.reconnects == 0


def test_an_inline_stall_climbs_the_ladder_to_the_typed_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: let ``_run_inline`` re-raise ``ClioCoreFutureTimeout`` and this fails."""
    monkeypatch.setenv("CLIO_ARC_LIVENESS_RETRIES", "1")
    monkeypatch.setenv("CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S", "0.01")
    monkeypatch.setenv("CLIO_ARC_LIVENESS_BACKOFF_MAX_S", "0.01")
    store = _Store(cached=True)

    def stall() -> None:
        raise ClioCoreFutureTimeout("put", "no_progress", 30.0)

    with pytest.raises(ClioCoreRuntimeLostError):
        rpc_liveness.guarded_store_rpc(store, "put", stall, inline_kind="segments")
    assert store.reconnects >= 1
    assert store.stalled == [rpc_liveness.RPC_STALLED_REASON]


def test_the_first_poll_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.arc import clio_core_async_ops

    clio_core_async_ops._first_poll_s.cache_clear()
    monkeypatch.setenv("CLIO_ARC_LIVENESS_FIRST_POLL_S", "0.002")
    try:
        assert clio_core_async_ops._first_poll_s() == 0.002
    finally:
        clio_core_async_ops._first_poll_s.cache_clear()
