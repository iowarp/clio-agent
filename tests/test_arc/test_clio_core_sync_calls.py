"""The opt-in synchronous blob calls (iowarp-core 2.5 ``Client.PutBlob/GetBlob/GetBlobSize``)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops
from clio_agent.arc import clio_core_sync_calls as sync_calls


class _Client:
    """Records which form each call used; the async forms fail the test."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {"present": b"abcdef"}
        self.calls: list[str] = []

    def GetBlobSize(self, _tag: Any, name: str) -> int | None:  # noqa: N802 - native shape
        self.calls.append("GetBlobSize")
        return len(self.blobs[name]) if name in self.blobs else None

    def GetBlob(self, _tag: Any, name: str, size: int, off: int) -> bytes | None:  # noqa: N802
        self.calls.append("GetBlob")
        data = self.blobs.get(name)
        return None if data is None else data[off : off + size]

    def PutBlob(self, _tag: Any, name: str, data: bytes, off: int) -> int:  # noqa: N802
        self.calls.append("PutBlob")
        if name == "refused":
            return 28
        old = self.blobs.get(name, b"")
        self.blobs[name] = old[:off].ljust(off, b"\0") + data + old[off + len(data) :]
        return 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("Async"):
            raise AssertionError(f"{name} used while blob_calls=sync")
        raise AttributeError(name)


@pytest.fixture
def sync_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sync_calls.mode.cache_clear()
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_BLOB_CALLS", "sync")
    monkeypatch.setattr(ops, "ensure_room", lambda _store, _name: None)
    yield
    sync_calls.mode.cache_clear()


def _store(client: _Client) -> SimpleNamespace:
    return SimpleNamespace(_client=client, _tag_ids=SimpleNamespace(get=lambda _k: "tag"))


def test_the_default_is_the_bounded_async_form(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_calls.mode.cache_clear()
    monkeypatch.delenv("CLIO_ARC_CLIO_CORE_BLOB_CALLS", raising=False)
    try:
        assert sync_calls.mode() == sync_calls.ASYNC
        assert not sync_calls.enabled(_Client())
    finally:
        sync_calls.mode.cache_clear()


def test_an_unknown_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_calls.mode.cache_clear()
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_BLOB_CALLS", "fast")
    try:
        with pytest.raises(ValueError, match="blob_calls"):
            sync_calls.mode()
    finally:
        sync_calls.mode.cache_clear()


def test_sync_mode_reads_sizes_and_bytes_with_the_synchronous_calls(sync_mode: None) -> None:
    """SABOTAGE: drop the branch in ``store_read`` and ``AsyncGetBlob`` fails the test."""
    client = _Client()
    store = _store(client)
    assert ops.store_blob_size(store, "segments", "present") == 6
    assert ops.store_blob_size(store, "segments", "missing") == 0
    assert ops.store_read(store, "segments", "present", 3, 2) == b"cde"
    assert client.calls == ["GetBlobSize", "GetBlobSize", "GetBlob"]


def test_a_failed_synchronous_read_raises(sync_mode: None) -> None:
    with pytest.raises(RuntimeError, match="GetBlob operation failed"):
        ops.store_read(_store(_Client()), "segments", "missing", 4)


def test_sync_mode_writes_through_put_blob_and_keeps_the_refusal_code(sync_mode: None) -> None:
    client = _Client()
    writer = ops.AsyncPutTag(client, "tag", store=object())
    writer.PutBlob("new", b"xyz", 0)
    assert client.blobs["new"] == b"xyz"
    with pytest.raises(ops.ClioCorePutRefused) as refused:
        writer.PutBlob("refused", b"x", 0)
    assert refused.value.native_code == 28


def test_a_client_without_the_calls_keeps_the_async_form(sync_mode: None) -> None:
    """iowarp-core 2.4 has no ``Client.GetBlobSize``: the flag cannot break it."""
    assert not sync_calls.enabled(SimpleNamespace(AsyncGetBlobSize=lambda *_a: None))
