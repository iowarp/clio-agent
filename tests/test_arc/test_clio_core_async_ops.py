"""Unit tests for the GIL-free clio-core store operations (fake futures, no daemon)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops

#: ``ready_after`` this large: the task never completes (a stuck or gone daemon).
_NEVER = 10**9


class _Future:
    """The native contract (iowarp-core 2.4): ``wait(max_sec)`` blocks, GIL released,
    until the task completes or ``max_sec`` elapses, then returns its code or ``None``."""

    def __init__(self, code: int = 0, ready_after: int = 0) -> None:
        self.code = code
        self.polls = 0
        self.waits = 0
        self.ready_after = ready_after

    def done(self) -> bool:
        self.polls += 1
        return self.polls > self.ready_after

    def wait(self, max_sec: float = -1.0) -> int | None:
        self.waits += 1
        if self.ready_after >= _NEVER:
            time.sleep(max(0.0, max_sec))
            return None
        self.polls = max(self.polls, self.ready_after)  # it completes during the wait
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

    def wait(self, max_sec: float = -1.0) -> int | None:
        until = self.ready_at if max_sec < 0 else min(self.ready_at, time.monotonic() + max_sec)
        time.sleep(max(0.0, until - time.monotonic()))
        return self.code if self.done() else None


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


def test_await_future_blocks_in_the_native_wait_until_done() -> None:
    """A pending put is waited for in ``Future.wait`` (GIL released), not by waking to
    poll ``done()``: each wake retook the interpreter lock from the agent's own threads.

    SABOTAGE: drop the ``wait`` call in ``_polled.done_within`` and the future is polled
    (``waits`` stays 0)."""
    future = _Future(ready_after=3)
    assert ops.await_future(future, op_name="put") is future
    assert future.waits == 1


@pytest.mark.parametrize("operation", ["put", "delete", "tag_query"])
def test_fast_native_reply_needs_no_daemon_discovery_or_configuration_reads(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A sub-millisecond RPC must not pay a process census on every history atom."""
    from clio_agent.arc import daemon_progress

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def unexpected() -> Any:
        pytest.fail("A fast completed native RPC performed slow health discovery")

    monkeypatch.setattr(ops, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(ops, "resolve_liveness_policy", unexpected)
    monkeypatch.setattr(daemon_progress, "max_wait_s", unexpected)
    monkeypatch.setattr(daemon_progress, "daemon_work", unexpected)
    future = SimpleNamespace(done=lambda: clock[0] >= 0.0005)
    assert ops.await_future(future, op_name=operation) is future
    assert 0 < clock[0] <= 0.001


def test_a_reply_within_the_sample_window_takes_no_progress_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A put a busy daemon answers in 30 ms is polled, not measured.

    The baseline sample (pidfile and ``/proc`` reads) was taken after a 1 ms miss, so it
    ran on most puts while the daemon was busy (P7, 2026-11-02). SABOTAGE: poll for
    1 ms before sampling again and ``daemon_work`` is called.
    """
    from clio_agent.arc import daemon_progress

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def unexpected() -> Any:
        pytest.fail("A reply inside the sample window measured the daemon's progress")

    monkeypatch.setattr(ops, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(daemon_progress, "daemon_work", unexpected)
    ops._sample_after_s.cache_clear()
    monkeypatch.setenv("CLIO_ARC_LIVENESS_SAMPLE_AFTER_S", "0.1")
    try:
        future = SimpleNamespace(done=lambda: clock[0] >= 0.03)
        assert ops.await_future(future, op_name="put") is future
    finally:
        ops._sample_after_s.cache_clear()


def test_clear_submits_independent_deletes_before_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large cleanup must not pay one round trip before submitting the next deletion."""
    client = _Client()
    store = SimpleNamespace(_client=client, _tag_ids=SimpleNamespace(get=lambda _kind: "tag"))
    original = ops.await_future

    def await_submitted(future: Any, *, op_name: str) -> Any:
        assert client.dels == [("tag", "first"), ("tag", "second"), ("tag", "third")]
        return original(future, op_name=op_name)

    monkeypatch.setattr(ops, "await_future", await_submitted)
    ops.store_delete_many(store, "segments", ("first", "second", "third"))


def test_clear_drains_submitted_deletes_before_reporting_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed observation leaves no silently abandoned members or resubmissions."""
    submitted: list[str] = []
    observed: list[str] = []

    def delete(_tag: str, name: str) -> Any:
        submitted.append(name)
        return SimpleNamespace(name=name, wait=lambda _timeout: 0)

    def wait(future: Any, *, op_name: str) -> Any:
        observed.append(future.name)
        if future.name == "second":
            raise OSError("lost delete reply")
        return future

    store = SimpleNamespace(
        _client=SimpleNamespace(AsyncDelBlob=delete),
        _tag_ids=SimpleNamespace(get=lambda _kind: "tag"),
    )
    monkeypatch.setattr(ops, "await_future", wait)
    with pytest.raises(ExceptionGroup, match="Core deletion batch failed") as failure:
        ops.store_delete_many(store, "segments", ("first", "second", "third"))
    assert submitted == observed == ["first", "second", "third"]
    assert len(failure.value.exceptions) == 1


@pytest.mark.parametrize("ready_at", [0.002, 0.5])
def test_polling_finishes_promptly_without_busy_spinning(
    monkeypatch: pytest.MonkeyPatch, ready_at: float
) -> None:
    """Fast local replies avoid an idle tick; longer replies keep bounded polling work."""
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ops.time, "sleep", sleep)
    future = SimpleNamespace(done=lambda: clock[0] >= ready_at)
    assert ops._polled(future)(1.0)
    if ready_at < 0.005:
        assert clock[0] < 0.005, "A fast reply paid a full idle polling tick"
    else:
        assert len(sleeps) < 70, "A pending call kept the fast-reply polling rate"
    assert all(0 < seconds <= 0.01 for seconds in sleeps)


def test_polling_does_not_sleep_past_its_health_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff does not extend the existing caller's deadline."""
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ops.time, "sleep", sleep)
    assert not ops._polled(SimpleNamespace(done=lambda: False))(0.0025)
    assert clock[0] == 0.0025


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
        ops.await_future(_Future(ready_after=_NEVER), op_name="put")
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
            return _Future(ready_after=_NEVER)

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
