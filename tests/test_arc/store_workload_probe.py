"""Measure real storage calls without replacing their persistence behavior."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest


class StoreWorkloadProbe:
    """Retain counts, byte volume and elapsed time around the actual store methods."""

    def __init__(self, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started = time.perf_counter()
        self.operations: dict[str, dict[str, float]] = {}
        for name in ("get", "get_raw", "put", "put_at", "put_if_absent", "put_many"):
            monkeypatch.setattr(store, name, self._wrap(name, getattr(store, name)))

    def _wrap(self, operation: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def measured(kind: str, *args: Any, **kwargs: Any) -> Any:
            records = args[0] if operation == "put_many" else ()
            names = [row.name for row in records] if records else [str(args[0])]
            group = (
                "index"
                if any(name.replace("~", "/").endswith("/_index") for name in names)
                else kind
            )
            stats = self.operations.setdefault(
                f"{operation}:{group}", {"calls": 0, "bytes": 0, "seconds": 0, "max_s": 0}
            )
            stats["calls"] += 1
            if operation in ("put", "put_at", "put_if_absent"):
                stats["bytes"] += len(args[1])
            elif operation == "put_many":
                stats["bytes"] += sum(len(row.data) for row in records)
            started = time.perf_counter()
            try:
                return original(kind, *args, **kwargs)
            finally:
                elapsed = time.perf_counter() - started
                stats["seconds"] += elapsed
                stats["max_s"] = max(stats["max_s"], elapsed)

        return measured

    def report(self, stage: str) -> None:
        """Flush bounded diagnostics before a native or whole-test watchdog can fire."""
        print(
            json.dumps(
                {
                    "storage_stage": stage,
                    "elapsed_s": time.perf_counter() - self.started,
                    "operations": self.operations,
                }
            ),
            flush=True,
        )
