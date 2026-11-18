"""The agent sees free space and each source's bytes, hard links counted once."""

from __future__ import annotations

import os
from pathlib import Path

from clio_agent.gact.storage.storage_usage import bytes_on_disk, storage_usage


def test_linked_snapshots_count_once(tmp_path: Path) -> None:
    store = tmp_path / "store"
    first = store / "source_a" / "snapshot_1"
    second = store / "source_a" / "snapshot_2"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "big.bin").write_bytes(b"x" * 1000)
    os.link(first / "big.bin", second / "big.bin")
    (second / "new.bin").write_bytes(b"y" * 10)
    assert bytes_on_disk(store / "source_a") == 1010
    usage = storage_usage(store, ["source_a", "source_missing"])
    assert usage["sources"] == {"source_a": 1010}
    assert usage["free_bytes"] > 0 and usage["total_bytes"] >= usage["free_bytes"]
