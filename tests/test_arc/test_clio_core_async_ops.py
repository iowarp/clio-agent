"""Unit tests for the GIL-free clio-core store operations (fake futures, no daemon)."""

from __future__ import annotations

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


def test_await_future_polls_until_done() -> None:
    future = _Future(ready_after=3)
    assert ops.await_future(future, op_name="put", timeout_s=5) is future
    assert future.polls == 4


def test_await_future_gives_up_typed_at_its_bound() -> None:
    with pytest.raises(ops.ClioCoreFutureTimeout, match="async put did not complete within 0.05s"):
        ops.await_future(_Future(ready_after=10**9), op_name="put", timeout_s=0.05)


def test_the_default_give_up_bound_is_past_the_stall_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.05")
    with pytest.raises(ops.ClioCoreFutureTimeout, match="within 1.05s"):
        ops.await_future(_Future(ready_after=10**9), op_name="put")


def test_tag_ids_resolve_once_per_kind_until_cleared() -> None:
    cte = _Cte()
    ids = ops.TagIds(cte)
    first = ids.get("records")
    assert ids.get("records") == first
    assert cte.tag_calls == ["records"]
    ids.clear()
    assert ids.get("records") != first  # re-resolved after a reconnect
    assert cte.tag_calls == ["records", "records"]


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
