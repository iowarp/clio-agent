"""An append's clio-core puts overlap (Phase 11a follow-up), on real clio-core.

A working-set append writes three independent blobs: the content-lane chunk, the
search-companion chunk body and its text. They are issued together on the async put
path (``AsyncPutBlob``) and awaited together; only the session index -- which must
never list less than is stored -- is put before them when it changes. A refused blob
is a typed error raised after every put of the batch completed, naming what was
written.

The in-flight count is taken the way the bench counts native calls: a proxy over the
store's client that wraps each ``AsyncPutBlob`` future.

Sabotage: issuing the batch one blob at a time (await each future before the next
``AsyncPutBlob``) turns ``test_an_appends_puts_are_in_flight_together`` red.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import ClioCoreStore, make_arc_store


class _Future:
    """Forwards a native future; counts it in flight until it reports done."""

    def __init__(self, future: Any, probe: _InFlight) -> None:
        self._future = future
        self._probe = probe
        self._open = True

    def done(self) -> bool:
        finished = bool(self._future.done())
        if finished:
            self._probe.close(self)
        return finished

    def wait(self, timeout: int) -> Any:
        code = self._future.wait(timeout)
        self._probe.close(self)
        return code


class _InFlight:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.issued: list[str] = []

    def open(self, name: str) -> None:
        with self.lock:
            self.issued.append(name)
            self.now += 1
            self.peak = max(self.peak, self.now)

    def close(self, future: _Future) -> None:
        with self.lock:
            if future._open:
                future._open = False
                self.now -= 1


class _Client:
    def __init__(self, client: Any, probe: _InFlight, refuse: str = "") -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_probe", probe)
        object.__setattr__(self, "_refuse", refuse)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name != "AsyncPutBlob":
            return attr

        def put(tag_id: Any, blob: str, data: Any, off: int = 0) -> _Future:
            self._probe.open(blob)
            if self._refuse and self._refuse in blob:
                return _Future(_Refused(), self._probe)
            return _Future(attr(tag_id, blob, data, off), self._probe)

        return put


class _Refused:
    """A native future that answers a non-zero code (a refused write)."""

    def done(self) -> bool:
        return True

    def wait(self, _timeout: int) -> int:
        return 13


@pytest.fixture
def store():
    s = make_arc_store(backend="cte", namespace=f"ov-{uuid.uuid4().hex[:8]}")
    assert isinstance(s, ClioCoreStore)
    yield s
    s.clear()


def test_an_appends_puts_are_in_flight_together(store: ClioCoreStore) -> None:
    arc = ARCMemory(store=store)
    sid = "ov_" + uuid.uuid4().hex[:8]
    arc.append_segment(sid, "agentA", "thought", {"text": "first"})  # opens chunk + index
    probe = _InFlight()
    store._client = _Client(store._client, probe)
    arc.append_segment(sid, "agentA", "observation", {"text": "second"})
    assert len(probe.issued) == 3, probe.issued  # lane chunk + search body + search text
    assert probe.peak == 3, "the append's puts did not overlap"
    texts = [s.content["text"] for s in ARCMemory(store=store).render_segments(sid, "agentA")]
    assert texts == ["first", "second"]


def test_a_refused_blob_fails_typed_after_the_batch_naming_what_was_written(
    store: ClioCoreStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_WRITE_RETRY_ATTEMPTS", "1")
    probe = _InFlight()
    real = store._client
    store._client = _Client(real, probe, refuse="refused-blob")
    records = [
        PutRecord(name="ok-blob", data=b"one"),
        PutRecord(name="refused-blob", data=b"two"),
        PutRecord(name="ok-blob-2", data=b"three", search_text="text"),
    ]
    with pytest.raises(BatchPutError) as caught:
        store.put_many("segments", records)
    assert caught.value.failed_names == ["refused-blob"]
    assert set(caught.value.written) == {"ok-blob", "ok-blob-2", "ok-blob-2.text"}
    store._client = real
    assert store.get("segments", "ok-blob-2") == b"three"  # the rest of the batch landed
