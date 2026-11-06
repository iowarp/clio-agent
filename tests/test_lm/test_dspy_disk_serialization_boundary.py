"""Qualify the pinned SDK's opt-in restricted cache, not its unsafe default."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from dspy.clients.cache import Cache
from dspy.clients.disk_serialization import DeserializationError


class _ExecutableCacheValue:
    """A benign pickle that would evaluate arithmetic if unrestricted."""

    def __reduce__(self) -> tuple[Callable[[str], object], tuple[str]]:
        return eval, ("1 + 1",)


def test_restricted_dspy_cache_rejects_executable_pickle_values(tmp_path: Path) -> None:
    """Exercise the explicit restriction with real DiskCache and a valid data read."""
    cache = Cache(
        enable_disk_cache=True,
        enable_memory_cache=False,
        disk_cache_dir=str(tmp_path / "dspy-cache"),
        restrict_pickle=True,
    )
    try:
        cache.disk_cache["data"] = {"measurements": [1, 2, 3]}
        assert cache.disk_cache["data"] == {"measurements": [1, 2, 3]}
        cache.disk_cache["untrusted"] = _ExecutableCacheValue()
        with pytest.raises(DeserializationError, match=r"builtins\.eval"):
            cache.disk_cache["untrusted"]
    finally:
        cache.disk_cache.close()
