"""Unit tests for the GIL-free clio-core store operations (fake futures, no daemon)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops


class _Future:
    def __init__(self, code: int = 0, ready_after: int = 0) -> None:
        self.code = code
        self.polls = 0
        self.ready_after = ready_after

    def done(self) -> bool:
        self.polls += 1
        return self.polls > self.ready_after

    def wait(self, max_sec: float = -1.0) -> int:
        assert self.polls > self.ready_after, "wait() on an unfinished Future can block forever"
        return self.code


class _Client:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.puts: list[tuple[Any, str, bytes]] = []
        self.dels: list[tuple[Any, str]] = []

    def AsyncPutBlob(self, tag_id: Any, name: str, data: bytes, off: int = 0) -> _Future:  # noqa: N802
        self.puts.append((tag_id, name, data))
        return _Future(self.code, ready_after=2)

    def AsyncDelBlob(self, tag_id: Any, name: str) -> _Future:  # noqa: N802
        self.dels.append((tag_id, name))
        return _Future(self.code)


class _Cte:
    def __init__(self) -> None:
        self.tag_calls: list[str] = []

    def Tag(self, name: str) -> SimpleNamespace:  # noqa: N802
        self.tag_calls.append(name)
        return SimpleNamespace(GetTagId=lambda: f"id-{name}-{len(self.tag_calls)}")


class _SlowFuture:
    """Done after ``seconds`` of wall time (a slow daemon answering late)."""

    def __init__(self, seconds: float, code: int = 0) -> None:
        self.code = code
        self.ready_at = time.monotonic() + seconds

    def done(self) -> bool:
        return time.monotonic() >= self.ready_at

    def wait(self, max_sec: float = -1.0) -> int:
        assert self.done(), "wait() on an unfinished Future can block forever"
        return self.code


def _daemon(monkeypatch: pytest.MonkeyPatch, read: Any) -> None:
    """The daemon progress signal (no real daemon is attached in these unit tests)."""
    from clio_agent.arc import daemon_progress

    monkeypatch.setattr(daemon_progress, "daemon_work", read)


def _working() -> Any:
    state = {"w": 0.0}

    def read() -> float:
        state["w"] += 1.0
        return state["w"]

    return read


def test_await_future_polls_until_done() -> None:
    future = _Future(ready_after=3)
    assert ops.await_future(future, op_name="put") is future
    assert future.polls == 4


def test_a_slow_future_with_a_working_daemon_is_waited_for_past_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fixed 31 s: a write answering after several no-progress windows while the
    daemon works completes.

    **Sabotage:** a fixed ``stall_after_s + 1`` give-up bound -> ClioCoreFutureTimeout.
    """
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.1")
    _daemon(monkeypatch, _working())
    future = _SlowFuture(2.5)  # past two worker-side windows (0.1 s + the 1 s margin)
    assert ops.await_future(future, op_name="put") is future


def test_a_future_whose_daemon_made_no_progress_ends_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.1")
    _daemon(monkeypatch, lambda: 5.0)
    with pytest.raises(ops.ClioCoreFutureTimeout) as info:
        ops.await_future(_Future(ready_after=10**9), op_name="put")
    assert info.value.reason == "no_progress"
    assert info.value.op_name == "put"
    assert isinstance(info.value, TimeoutError)
    assert not isinstance(info.value, RuntimeError)  # never read as a refusal


def test_a_timed_out_put_is_neither_reissued_nor_recorded_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending PutBlob is not a refusal: the retry helper must not re-issue it (the
    first may still land) nor record a lost write; the stall surfaces typed.

    **Sabotage:** make ``ClioCoreFutureTimeout`` a ``RuntimeError`` again -> the retry
    re-issues AsyncPutBlob and records a lost write.
    """
    from clio_agent.arc import clio_core_retry

    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.1")
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S", "0")
    _daemon(monkeypatch, lambda: 5.0)
    clio_core_retry._reset_put_write_health_for_tests()

    class _HungClient(_Client):
        def AsyncPutBlob(self, tag_id: Any, name: str, data: bytes, off: int = 0) -> _Future:  # noqa: N802
            self.puts.append((tag_id, name, data))
            return _Future(ready_after=10**9)

    store = SimpleNamespace(_client=_HungClient(), _tag_ids=ops.TagIds(_Cte()))
    with pytest.raises(ops.ClioCoreFutureTimeout):
        ops.store_put(store, "records", "a", b"payload")
    assert len(store._client.puts) == 1
    with pytest.raises(ops.ClioCoreFutureTimeout):
        ops.store_put_many(store, "records", [("b", b"1"), ("c", b"2")])
    assert [name for _tag, name, _data in store._client.puts] == ["a", "b", "c"]
    assert clio_core_retry.last_lost_put_write() is None


def test_tag_ids_resolve_once_per_kind_until_cleared() -> None:
    cte = _Cte()
    ids = ops.TagIds(cte)
    first = ids.get("records")
    assert ids.get("records") == first
    assert cte.tag_calls == ["records"]
    ids.clear()
    assert ids.get("records") != first  # re-resolved after a reconnect
    assert cte.tag_calls == ["records", "records"]


def test_tag_ids_prewarm_supported_kinds() -> None:
    cte = _Cte()
    ids = ops.TagIds(cte)
    ids.prewarm(("conversations", "segments"))
    assert cte.tag_calls == ["conversations", "segments"]
    ids.get("segments")
    assert cte.tag_calls == ["conversations", "segments"]


def test_store_put_writes_async_and_a_refusal_raises_for_the_retry_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cte = _Cte()
    store = SimpleNamespace(_client=_Client(), _tag_ids=ops.TagIds(cte))
    ops.store_put(store, "records", "a", b"payload")
    assert store._client.puts == [("id-records-1", "a", b"payload")]

    store._client = _Client(code=3)
    with pytest.raises(RuntimeError, match="returned code 3"):
        ops.AsyncPutTag(store._client, "tid").PutBlob("a", b"x")


def test_store_delete_reports_whether_the_blob_existed() -> None:
    cte = _Cte()
    store = SimpleNamespace(_client=_Client(), _tag_ids=ops.TagIds(cte))
    assert ops.store_delete(store, "records", "a") is True
    store._client = _Client(code=1)  # a missing blob answers non-zero: a no-op, not an error
    assert ops.store_delete(store, "records", "missing") is False
