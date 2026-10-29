"""Cache eviction must allow a writer's nested scope lookup to finish."""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Any, cast

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.segments import SegmentStore


class _ScopeProbe:
    """Bound a broken release's lock wait so the regression cannot leak threads."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.attempted = threading.Event()

    def acquire(self) -> bool:
        if threading.current_thread().name == "cache-drop":
            self.attempted.set()
            if not self.lock.acquire(timeout=3):
                raise TimeoutError("cache drop blocked the writer's registry lookup")
        else:
            self.lock.acquire()
        return True

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> _ScopeProbe:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@pytest.mark.parametrize("operation", ["release", "clear"])
@pytest.mark.parametrize("new_scope", [False, True])
def test_drop_allows_nested_lookup_and_rechecks_new_scope(
    operation: str, new_scope: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SegmentStore(_MemoryStore())
    store.append("session", "base", "thought", {"text": "committed"})
    probe = _ScopeProbe()
    monkeypatch.setitem(store._scope_locks, ("session", "base"), cast(Any, probe))
    errors: list[BaseException] = []

    def drop() -> None:
        try:
            if operation == "release":
                store.release("session")
            else:
                store.clear()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=drop, name="cache-drop")
    with probe:
        worker.start()
        assert probe.attempted.wait(3), "cache drop did not reach the scope lock"
        # Lane writers already hold a base lock when they look up a chunk lock.
        assert store.render("session", "base")[0].content["text"] == "committed"
        if new_scope:
            store.append("session", "base/chunk", "thought", {"text": "later"})
    worker.join(3)
    assert not worker.is_alive(), "cache drop did not retire"
    assert errors == []
    assert not store._scopes
    assert store.render("session", "base")[0].content["text"] == "committed"
    if new_scope:
        assert store.render("session", "base/chunk")[0].content["text"] == "later"
