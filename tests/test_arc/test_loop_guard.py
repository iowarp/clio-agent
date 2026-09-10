"""The SERVER-loop store guard (#1334): a store write waited on from the thread running
the SERVER's loop is a typed defect at the persist seam; a read is audited, not refused.

Which loop matters. A write on a PRIVATE loop (a provider's ``asyncio.run`` on a worker
thread) blocks only that worker, and refusing it DROPPED nine semantic events per live run
(``arc.memory.record_semantic_event`` swallows the raise). So once the app's lifespan has
registered its loop the guard keys on identity, a draining server loop is allowed (a
shutdown flush must land), and with nothing registered it stays strict so a bare
``asyncio.run`` in a unit test still catches a real write site.

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
import threading
from typing import Any

import pytest

import clio_agent.arc.loop_guard as loop_guard
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.loop_guard import (
    STORE_READ_ON_LOOP_THREAD,
    STORE_WRITE_ON_DRAINING_LOOP,
    STORE_WRITE_ON_LOOP_THREAD,
    STORE_WRITE_ON_PRIVATE_LOOP,
    LoopThreadStoreWrite,
    assert_store_write_off_loop,
    audit_store_read_on_loop,
    begin_server_loop_drain,
    on_server_loop,
    register_server_loop,
    unregister_server_loop,
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


# --------------------------------------------------------------------------- #
# Loop IDENTITY: only the SERVER's loop is refused (#1334 follow-up)           #
# --------------------------------------------------------------------------- #


def _run_a_registered_server_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Start a loop on its own thread and register it as THE server loop."""

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    register_server_loop(loop)
    return loop, thread


def _stop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    unregister_server_loop(loop)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_a_nested_asyncio_run_on_a_worker_thread_writes_fine(monkeypatch: Any) -> None:
    """The dropped-events regression: a PRIVATE loop is audited and ALLOWED.

    ``lm/io_logging.py::_clio_streamed_call`` drives each provider call under its own
    ``asyncio.run`` on an anyio worker and emits ``lm.call`` from inside it. Blocking
    there blocks only that worker; refusing it lost the event.
    """

    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(loop_guard, "stream_audit", lambda stage, **f: rows.append((stage, f)))
    store = SegmentStore(_MemoryStore())
    loop, thread = _run_a_registered_server_loop()
    try:

        def _worker() -> None:
            async def _on_private_loop() -> None:
                assert on_server_loop() is False
                _append(store)

            asyncio.run(_on_private_loop())

        worker = threading.Thread(target=_worker)
        worker.start()
        worker.join(timeout=10)
    finally:
        _stop(loop, thread)

    assert store.render("sess", "scope-a"), "the private-loop write never landed"
    assert [r[0] for r in rows] == ["store.write_on_private_loop"]
    assert rows[0][1]["reason"] == STORE_WRITE_ON_PRIVATE_LOOP
    assert [hit[0] for hit in loop_guard.guard_hits()][-1] == STORE_WRITE_ON_PRIVATE_LOOP


def test_a_write_on_the_registered_server_loop_still_raises() -> None:
    """Identity, not leniency: the loop the server serves on is refused as before."""

    store = SegmentStore(_MemoryStore())
    loop, thread = _run_a_registered_server_loop()
    try:

        async def _on_server_loop() -> None:
            assert on_server_loop() is True
            _append(store)

        future = asyncio.run_coroutine_threadsafe(_on_server_loop(), loop)
        with pytest.raises(LoopThreadStoreWrite) as info:
            future.result(timeout=10)
        assert info.value.error_type == STORE_WRITE_ON_LOOP_THREAD
    finally:
        _stop(loop, thread)


def test_a_shutdown_flush_on_a_draining_server_loop_lands(monkeypatch: Any) -> None:
    """After the lifespan's ``yield`` nothing is served, so teardown writes must LAND."""

    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(loop_guard, "stream_audit", lambda stage, **f: rows.append((stage, f)))
    store = SegmentStore(_MemoryStore())
    loop, thread = _run_a_registered_server_loop()
    try:

        async def _teardown_flush() -> None:
            begin_server_loop_drain(loop)
            assert on_server_loop() is False
            _append(store)

        asyncio.run_coroutine_threadsafe(_teardown_flush(), loop).result(timeout=10)
    finally:
        _stop(loop, thread)

    assert store.render("sess", "scope-a"), "the shutdown flush was dropped"
    assert [r[0] for r in rows] == ["store.write_on_draining_loop"]
    assert rows[0][1]["reason"] == STORE_WRITE_ON_DRAINING_LOOP


def test_with_no_server_loop_registered_the_guard_stays_strict() -> None:
    """The unit-test / CLI default: any running loop is treated as the server's."""

    assert on_server_loop() is False
    store = SegmentStore(_MemoryStore())

    async def _on_a_bare_loop() -> None:
        _append(store)

    with pytest.raises(LoopThreadStoreWrite):
        asyncio.run(_on_a_bare_loop())
