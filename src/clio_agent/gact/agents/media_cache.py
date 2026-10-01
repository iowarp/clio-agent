"""Rehydrated tool media, cached by the descriptor's sha256 per path.

A ``view_image`` / ``view_pdf`` result is recorded as a descriptor (path, snapshot,
sha256) and hydrated into an inline media part whenever the context is folded. The
hydration reads the snapshot (or workspace file), checks its sha256 against the
descriptor -- the integrity check, unchanged -- and base64-encodes it. Bytes that
passed that check under one (workspace, path, snapshot, sha256) are the bytes the
descriptor names, so the hydrated part is kept and reused while the file it was read
from is unchanged on disk (same size and mtime): a context rebuilt after an op does
not read, hash and encode every earlier image again. A file that changed or is gone
misses the cache and goes through the hydration (and its check) again. A failed
hydration is never cached (the caller turns it into its ``media_unavailable`` note).

The cache is bounded by the base64 bytes it holds (``gact.media_cache_bytes``), least
recently used first out.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from clio_agent import conf

__all__ = ["cached_media", "clear_media_cache"]

_PartT = TypeVar("_PartT")

_LOCK = threading.Lock()
_ENTRIES: OrderedDict[tuple[str, ...], tuple[Any, int]] = OrderedDict()
_HELD = [0]  # base64 bytes held


def _budget() -> int:
    return conf.resolve(
        "gact.media_cache_bytes",
        env="CLIO_GACT_MEDIA_CACHE_BYTES",
        default=64 * 1024 * 1024,
        cast=conf.as_int,
    )


def _key(kind: str, descriptor: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The cache key: the file read, its on-disk state and the sha256 it must match.

    ``None`` (hydrate every time) when there is no sha256 to key the integrity on or the
    file cannot be stat'ed (the hydration then reports why).
    """
    from clio_agent.gact import viewed_media  # noqa: PLC0415

    sha = str(descriptor.get("snapshot_sha256") or descriptor.get("sha256") or "")
    relative = str(descriptor.get("snapshot") or descriptor.get("path") or "")
    if not sha or not relative:
        return None
    path = viewed_media._workspace_root() / relative
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        kind,
        str(path),
        str(stat.st_size),
        str(stat.st_mtime_ns),
        sha,
        str(descriptor.get("pages") or ""),
    )


def cached_media(
    kind: str,
    descriptor: Mapping[str, Any],
    hydrate: Callable[[], _PartT],
    size: Callable[[_PartT], int],
) -> _PartT:
    """The hydrated part for ``descriptor``: cached, or ``hydrate()`` (then cached).

    Args:
        kind: ``"image"`` or ``"document"`` (part of the key).
        descriptor: The recorded tool-result descriptor.
        hydrate: Reads, verifies and encodes the media (raises on any failure).
        size: The part's held size in bytes (its base64 length).

    Returns:
        The media part.
    """
    key = _key(kind, descriptor)
    if key is not None:
        with _LOCK:
            hit = _ENTRIES.get(key)
            if hit is not None:
                _ENTRIES.move_to_end(key)
                return hit[0]
    part = hydrate()
    if key is None:
        return part
    held = size(part)
    budget = _budget()
    with _LOCK:
        if key not in _ENTRIES and held <= budget:
            _ENTRIES[key] = (part, held)
            _HELD[0] += held
            while _HELD[0] > budget and _ENTRIES:
                _old, (_part, old_size) = _ENTRIES.popitem(last=False)
                _HELD[0] -= old_size
    return part


def clear_media_cache() -> None:
    """Forget every cached part."""
    with _LOCK:
        _ENTRIES.clear()
        _HELD[0] = 0
