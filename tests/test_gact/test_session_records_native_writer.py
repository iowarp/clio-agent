"""The session-records writer retires when idle, so its store calls run on a persistent
native thread: clio-core keeps an epoll and a signalfd for every thread that ever
called it, and a fresh writer after each idle spell would add a pair every time.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from clio_agent.gact import session_records
from clio_agent.gact.session_records import KIND, _Writer


class _ThreadRecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def put_many(self, kind: str, records: Any) -> None:
        assert kind == KIND
        self.calls.extend(("put", threading.current_thread().name) for _ in records)

    def delete(self, kind: str, name: str) -> None:
        self.calls.append(("delete", threading.current_thread().name))


def test_a_retiring_writer_enters_the_store_on_a_persistent_native_thread(
    monkeypatch: Any,
) -> None:
    """SABOTAGE: call ``self._apply(batch)`` directly in ``_Writer._run`` and the store
    is entered on the retiring ``clio-session-records`` thread."""
    monkeypatch.setattr(session_records, "_IDLE_SECONDS", 0.05)
    store = _ThreadRecordingStore()
    writer = _Writer(store)
    for round_ in range(2):  # the second round runs on a fresh writer thread
        ticket = writer.submit({f"a{round_}": b"x", f"b{round_}": None})
        assert writer.wait(ticket, 10.0)
        while writer._thread is not None:  # let this writer retire
            time.sleep(0.01)
    assert {kind for kind, _ in store.calls} == {"put", "delete"}
    assert {name for _, name in store.calls} == {"arc-native"}
