"""The loop-thread store guard (#1334): a store write waited on from a thread that runs
an asyncio loop is a typed defect at the persist seam; a read is audited, not refused.

Hermetic: the store is the in-memory backend, so the guard is proven at the seam every
backend shares (``SegmentStore._put_scope``), plus the real-store RPC seams
(``guarded_store_rpc`` / ``guard_store_op``) with a stub ``ClioCoreStore`` surface.
Sabotage anchor: delete the ``assert_store_write_off_loop`` call in ``_put_scope`` and the
first two tests go red.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

import pytest

import clio_agent.arc.loop_guard as loop_guard
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.loop_guard import (
    STORE_READ_ON_LOOP_THREAD,
    STORE_WRITE_ON_LOOP_THREAD,
    LoopThreadStoreWrite,
    assert_store_write_off_loop,
    audit_store_read_on_loop,
)
from clio_agent.arc.rpc_liveness import guard_store_op, guarded_store_rpc
from clio_agent.arc.segments import SegmentStore


def _append(store: SegmentStore) -> None:
    store.append("sess", "scope-a", "observation", {"text": "hello"}, step=1)


def test_write_from_a_loop_thread_raises_typed() -> None:
    store = SegmentStore(_MemoryStore())

    async def _on_loop() -> None:
        _append(store)

    with pytest.raises(LoopThreadStoreWrite) as info:
        asyncio.run(_on_loop())
    err = info.value
    assert err.error_type == STORE_WRITE_ON_LOOP_THREAD
    assert err.to_dict()["details"]["op"] == "segments.put"
    assert err.to_dict()["details"]["scope"] == "scope-a"


def test_write_from_a_worker_thread_while_a_loop_runs_elsewhere_passes() -> None:
    store = SegmentStore(_MemoryStore())

    async def _on_loop() -> None:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            await loop.run_in_executor(pool, _append, store)

    asyncio.run(_on_loop())
    assert store.render("sess", "scope-a")


def test_write_from_a_plain_thread_passes() -> None:
    store = SegmentStore(_MemoryStore())
    _append(store)
    assert store.render("sess", "scope-a")


def test_scope_drop_from_a_loop_thread_raises() -> None:
    store = SegmentStore(_MemoryStore())
    _append(store)

    async def _on_loop() -> None:
        store.drop_scope("sess", "scope-a")

    with pytest.raises(LoopThreadStoreWrite) as info:
        asyncio.run(_on_loop())
    assert info.value.op == "segments.delete"


def test_guard_writes_the_stream_audit_row(monkeypatch: Any) -> None:
    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(loop_guard, "stream_audit", lambda stage, **f: rows.append((stage, f)))

    async def _on_loop() -> None:
        assert_store_write_off_loop("put", scope="s1")

    with pytest.raises(LoopThreadStoreWrite):
        asyncio.run(_on_loop())
    assert rows == [
        (
            "store.write_on_loop_thread",
            {
                "op": "put",
                "scope": "s1",
                "thread": rows[0][1]["thread"],
                "reason": STORE_WRITE_ON_LOOP_THREAD,
            },
        )
    ]


def test_read_on_a_loop_thread_is_audited_not_refused(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(loop_guard, "stream_audit", lambda stage, **f: rows.append((stage, f)))
    monkeypatch.setattr(loop_guard, "_READ_WARNED", set())

    async def _on_loop() -> list[bool]:
        return [
            audit_store_read_on_loop("get", name="n1"),
            audit_store_read_on_loop("get", name="n2"),
        ]

    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.loop_guard"):
        hits = asyncio.run(_on_loop())
    assert hits == [True, True]
    assert [r[0] for r in rows] == ["store.read_on_loop_thread"] * 2
    assert rows[0][1]["reason"] == STORE_READ_ON_LOOP_THREAD
    # One warning per op name; the rest audit-only.
    assert sum("store read 'get'" in rec.getMessage() for rec in caplog.records) == 1
    assert audit_store_read_on_loop("get") is False  # no loop on this thread


class _StubStore:
    """The surface ``guard_store_op`` / ``guarded_store_rpc`` need from a ClioCoreStore."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._gate = type("Gate", (), {"port": 0, "note_rpc_stalled": lambda *_a, **_k: None})()

    def _live(self) -> None:
        self.calls.append("live")

    def _reconnect(self) -> None:
        self.calls.append("reconnect")

    @guard_store_op("get")
    def get(self, kind: str, name: str) -> bytes:
        return b"v"

    @guard_store_op("delete")
    def delete(self, kind: str, name: str) -> None:
        self.calls.append(f"delete:{name}")


def test_real_store_seams_refuse_writes_and_audit_reads_on_the_loop(monkeypatch: Any) -> None:
    rows: list[str] = []
    monkeypatch.setattr(loop_guard, "stream_audit", lambda stage, **f: rows.append(stage))
    store = _StubStore()

    async def _on_loop() -> None:
        assert store.get("segments", "k") == b"v"  # audited, served
        with pytest.raises(LoopThreadStoreWrite):
            store.delete("segments", "k")
        with pytest.raises(LoopThreadStoreWrite):
            guarded_store_rpc(store, "put", lambda: None)
        assert guarded_store_rpc(store, "search", lambda: 7) == 7

    asyncio.run(_on_loop())
    assert rows == [
        "store.read_on_loop_thread",
        "store.write_on_loop_thread",
        "store.write_on_loop_thread",
        "store.read_on_loop_thread",
    ]
    assert "delete:k" not in store.calls  # refused BEFORE the socket gate / RPC
