"""The lockfile digest is hashed once per lockfile state, not on every tool call.

Every tool call records its environment; hashing the ~1 MB ``uv.lock`` each time was
most of transform recording's file reads (P7, 2026-11-02).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.gact.artifacts import environment


def test_the_lockfile_is_hashed_again_only_after_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: call ``_sha256_file`` directly in ``_lockfile_hash`` and it is hashed
    three times."""
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    hashed: list[Path] = []
    original = environment._sha256_file

    def counting(path: Path) -> str | None:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(environment, "_lockfile_path", lambda: lock)
    monkeypatch.setattr(environment, "_sha256_file", counting)
    monkeypatch.setattr(environment, "_LOCKFILE_DIGESTS", {})

    first = environment._lockfile_hash()
    assert environment._lockfile_hash() == first
    assert len(hashed) == 1

    lock.write_text("version = 2\n", encoding="utf-8")
    stat = lock.stat()
    os.utime(lock, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert environment._lockfile_hash() != first
    assert len(hashed) == 2
