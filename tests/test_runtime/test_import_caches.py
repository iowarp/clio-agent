"""Import-cache refreshes are serialized, so concurrent provider checks never 500."""

from __future__ import annotations

import importlib
import threading
import time

import pytest

from clio_agent.runtime import import_caches


def test_concurrent_refreshes_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    guard = threading.Lock()

    def refresh() -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with guard:
            active -= 1

    monkeypatch.setattr(importlib, "invalidate_caches", refresh)
    threads = [threading.Thread(target=import_caches.invalidate_import_caches) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_an_entry_removed_outside_clio_finishes_the_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("C:\\uv\\python\\cpython-3.12\\python312.zip")

    monkeypatch.setattr(importlib, "invalidate_caches", refresh)
    import_caches.invalidate_import_caches()
    assert len(calls) == 2
