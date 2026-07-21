"""Disk bound for the clio-owned MCP uv spawn cache (iowarp/clio-agent#1001).

``tools.mcp_config._mcp_uv_cache_dir`` deliberately isolates every ``uvx``/``uv run``
MCP launcher onto a dedicated uv cache (astral-sh/uv#11694: a shared cache races on
concurrent cold spawns and a ``uv cache prune`` under a live server deletes ephemeral
envs). That isolation is locally correct but has NO eviction: uv builds one ephemeral
environment per spec-hash under ``environments-v*/`` and never deletes the old one, so
re-materializing the same scientific closure across spec bumps grows the cache without
bound (the 2026-07-20 audit measured this cache peaking at ~14 GB). Bounded RAM is
release-gating (#930); bounded DISK is the same class (#1001) — a desktop agent must not
demand tens of GB of cache from a user's machine.

This module gives that ONE clio-owned cache a **steady-state disk bound**, enforced
at server boot only. Two config-first knobs bound it:

* ``tools.mcp_cache.max_bytes`` (env ``CLIO_MCP_CACHE_MAX_BYTES``, default 2 GiB) — the
  total-size ceiling.
* ``tools.mcp_cache.max_age_days`` (env ``CLIO_MCP_CACHE_MAX_AGE_DAYS``, default 14) — the
  age ceiling for a single built environment.

The prune manages the ``environments-v*/`` ephemeral venvs (the unbounded growth), never
the shared wheel/sdist archive (uv owns that; ``clio doctor --gc`` runs ``uv cache prune``
for it). Two passes: (1) delete any env whose mtime is older than ``max_age_days``, then
(2) while the whole cache is over ``max_bytes``, evict the OLDEST remaining env until under
budget. Every eviction records a **typed reason** (:data:`_PRUNE_REASONS`, unknown reasons
rejected) with the bytes freed — mirroring the ``stream_fallback`` reason-catalog contract
in :mod:`clio_agent.gact.streaming` and the eviction catalog in
:mod:`clio_agent.gact.runtime.retention`. No silent degradation, ever.

**Boot-only, never mid-session.** A prune is safe exactly when the booting process owns
the cache — i.e. no OTHER clio process is alive that might be spawning into it
(astral-sh/uv#11694 is the whole reason the cache is isolated). :func:`boot_prune_mcp_cache`
consults an injected liveness prober and, if any peer is alive, SKIPS with a typed reason
rather than racing a concurrent spawn.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace

__all__ = [
    "MCPCacheBounds",
    "PrunedEntry",
    "PruneResult",
    "cache_bounds",
    "mcp_cache_max_bytes",
    "mcp_cache_max_age_days",
    "prune_uv_cache",
    "boot_prune_mcp_cache",
]

# Default bounds. Config-first (``tools.mcp_cache.*`` file namespace + ``CLIO_MCP_CACHE_*``
# env overrides); these are a safety ceiling, not a behavioural knob — the steady state a
# desktop app may reasonably keep for MCP spawn envs.
_DEFAULT_MAX_BYTES = 2 * 1024**3  # 2 GiB
_DEFAULT_MAX_AGE_DAYS = 14.0

# uv builds ephemeral ``uvx``/``uv run --with`` environments under this subtree (the ``v*``
# suffix is uv's cache-format version — matched by glob so a uv upgrade that bumps it does
# not silently disable the prune). These are the unbounded, clio-owned growth this module
# manages; the sibling ``wheels-v*`` / ``sdists-v*`` archive is uv's shared, dedup'd build
# input and is left to ``uv cache prune`` (run by ``clio doctor --gc``), never deleted here.
_ENV_SUBDIR_GLOB = "environments-v*"


# --------------------------------------------------------------------------- #
# Typed prune reason catalog (closed set; unknown reasons rejected)
# --------------------------------------------------------------------------- #

_PRUNE_REASONS: dict[str, dict[str, Any]] = {
    "mcp_cache_pruned_age": {
        "category": "mcp_cache_retention",
        "policy": "age_over_max_age_days",
        "description": (
            "An MCP uv-cache environment was older than the configured max age; it was "
            "evicted at boot. The next spawn that needs it rebuilds it lazily."
        ),
    },
    "mcp_cache_pruned_size": {
        "category": "mcp_cache_retention",
        "policy": "oldest_first_over_max_bytes",
        "description": (
            "The MCP uv-cache exceeded its configured size ceiling; the oldest "
            "environment was evicted (oldest-first) to bring the cache under budget."
        ),
    },
    "mcp_cache_prune_skipped_live": {
        "category": "mcp_cache_retention",
        "policy": "boot_only_never_mid_session",
        "description": (
            "An MCP uv-cache prune was requested but another clio process is alive and "
            "may be spawning into the cache; skipped to avoid racing a concurrent spawn "
            "(astral-sh/uv#11694). The prune runs at the next boot with no peers alive."
        ),
    },
    "mcp_cache_prune_over_budget": {
        "category": "mcp_cache_retention",
        "policy": "residual_over_budget_after_env_prune",
        "description": (
            "After evicting every eligible environment the cache is still over its size "
            "ceiling — the residual is uv's shared wheel/sdist archive. Run "
            "'clio doctor --gc' (which runs 'uv cache prune') to reclaim it."
        ),
    },
}


def prune_reason_catalog() -> dict[str, dict[str, Any]]:
    """Return the typed MCP-cache prune reason catalog (for capability metadata)."""
    return {reason: dict(details) for reason, details in _PRUNE_REASONS.items()}


# --------------------------------------------------------------------------- #
# Config-resolved bounds
# --------------------------------------------------------------------------- #


def mcp_cache_max_bytes() -> int:
    """The total-size ceiling for the MCP uv cache (config/env; #1001)."""
    value = int(
        conf.resolve(
            "tools.mcp_cache.max_bytes",
            env="CLIO_MCP_CACHE_MAX_BYTES",
            default=_DEFAULT_MAX_BYTES,
            cast=conf.as_int,
        )
    )
    if value <= 0:
        trace.event(
            "MCP-CACHE",
            "config tools.mcp_cache.max_bytes=%r invalid (must be > 0); using default %r",
            value,
            _DEFAULT_MAX_BYTES,
        )
        return _DEFAULT_MAX_BYTES
    return value


def mcp_cache_max_age_days() -> float:
    """The per-environment age ceiling for the MCP uv cache (config/env; #1001)."""
    value = float(
        conf.resolve(
            "tools.mcp_cache.max_age_days",
            env="CLIO_MCP_CACHE_MAX_AGE_DAYS",
            default=_DEFAULT_MAX_AGE_DAYS,
            cast=conf.as_float,
        )
    )
    if value <= 0:
        trace.event(
            "MCP-CACHE",
            "config tools.mcp_cache.max_age_days=%r invalid (must be > 0); using default %r",
            value,
            _DEFAULT_MAX_AGE_DAYS,
        )
        return _DEFAULT_MAX_AGE_DAYS
    return value


@dataclass(frozen=True)
class MCPCacheBounds:
    """The resolved disk bounds for the MCP uv cache."""

    max_bytes: int
    max_age_days: float


def cache_bounds() -> MCPCacheBounds:
    """Resolve the current MCP-cache bounds (config file → env → default)."""
    return MCPCacheBounds(max_bytes=mcp_cache_max_bytes(), max_age_days=mcp_cache_max_age_days())


# --------------------------------------------------------------------------- #
# Prune result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PrunedEntry:
    """One environment evicted (or that would be, under ``dry_run``)."""

    path: str
    reason: str
    bytes_freed: int
    age_days: float


@dataclass
class PruneResult:
    """Outcome of a single :func:`prune_uv_cache` pass."""

    cache_dir: str
    existed: bool
    dry_run: bool
    total_bytes_before: int = 0
    total_bytes_after: int = 0
    entries: list[PrunedEntry] = field(default_factory=list)
    over_budget_residual: bool = False

    @property
    def bytes_freed(self) -> int:
        """Total bytes reclaimed (or reclaimable, under ``dry_run``)."""
        return sum(e.bytes_freed for e in self.entries)

    @property
    def count(self) -> int:
        """Number of environments evicted."""
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable summary."""
        return {
            "cache_dir": self.cache_dir,
            "existed": self.existed,
            "dry_run": self.dry_run,
            "total_bytes_before": self.total_bytes_before,
            "total_bytes_after": self.total_bytes_after,
            "bytes_freed": self.bytes_freed,
            "count": self.count,
            "over_budget_residual": self.over_budget_residual,
            "entries": [
                {
                    "path": e.path,
                    "reason": e.reason,
                    "bytes_freed": e.bytes_freed,
                    "age_days": round(e.age_days, 3),
                }
                for e in self.entries
            ],
        }


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


def _dir_size(path: Path) -> int:
    """Sum the on-disk size of every regular file under ``path`` (symlinks not followed)."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                stat = os.lstat(fp)
            except OSError:
                continue
            total += stat.st_size
    return total


@dataclass(frozen=True)
class _EnvEntry:
    path: Path
    size: int
    mtime: float


def _iter_env_entries(cache_dir: Path) -> list[_EnvEntry]:
    """Enumerate the ephemeral-environment directories under ``environments-v*/``.

    Sorted OLDEST-first by mtime so an age/size prune evicts the least-recently-built
    environment first.
    """
    entries: list[_EnvEntry] = []
    for env_root in cache_dir.glob(_ENV_SUBDIR_GLOB):
        if not env_root.is_dir():
            continue
        for child in env_root.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            entries.append(_EnvEntry(path=child, size=_dir_size(child), mtime=mtime))
    entries.sort(key=lambda e: e.mtime)
    return entries


def prune_uv_cache(
    cache_dir: str | os.PathLike[str],
    *,
    max_bytes: int,
    max_age_days: float,
    now: float | None = None,
    dry_run: bool = False,
) -> PruneResult:
    """Bound a uv-style spawn cache to ``max_bytes`` + ``max_age_days`` (pure over a dir).

    Two passes over the ``environments-v*/`` ephemeral venvs (the shared wheel/sdist
    archive is never touched here):

    1. **Age**: evict every environment whose mtime is older than ``max_age_days``.
    2. **Size**: while the WHOLE cache still exceeds ``max_bytes``, evict the oldest
       remaining environment until under budget or none remain.

    Each eviction is recorded as a typed :class:`PrunedEntry`. Under ``dry_run`` nothing
    is deleted but the same plan (paths, reasons, bytes) is returned. If the cache is
    still over budget after every environment is gone, ``over_budget_residual`` is set (the
    residual is the shared archive — ``clio doctor --gc`` runs ``uv cache prune`` for it).

    Args:
        cache_dir: The uv cache directory to bound.
        max_bytes: Total-size ceiling. Must be > 0.
        max_age_days: Per-environment age ceiling in days. Must be > 0.
        now: Reference epoch seconds (injectable for tests); defaults to ``time.time()``.
        dry_run: When true, compute the plan without deleting anything.

    Returns:
        A :class:`PruneResult` describing what was (or would be) evicted.
    """
    path = Path(cache_dir)
    now = time.time() if now is None else now
    result = PruneResult(cache_dir=str(path), existed=path.is_dir(), dry_run=dry_run)
    if not result.existed:
        return result

    max_age_seconds = max_age_days * 86400.0
    entries = _iter_env_entries(path)
    archive_bytes = _dir_size(path) - sum(e.size for e in entries)
    result.total_bytes_before = archive_bytes + sum(e.size for e in entries)

    live: dict[Path, _EnvEntry] = {e.path: e for e in entries}

    def _evict(entry: _EnvEntry, reason: str) -> None:
        age_days = max((now - entry.mtime) / 86400.0, 0.0)
        if not dry_run:
            shutil.rmtree(entry.path, ignore_errors=True)
        live.pop(entry.path, None)
        result.entries.append(
            PrunedEntry(
                path=str(entry.path),
                reason=reason,
                bytes_freed=entry.size,
                age_days=age_days,
            )
        )
        trace.event(
            "MCP-CACHE",
            "%s%s path=%s freed=%d age_days=%.2f",
            "would-evict " if dry_run else "evicted ",
            reason,
            entry.path,
            entry.size,
            age_days,
        )

    # Pass 1 — age.
    for entry in entries:
        if now - entry.mtime > max_age_seconds:
            _evict(entry, "mcp_cache_pruned_age")

    # Pass 2 — size (oldest-first over the survivors).
    def _current_total() -> int:
        return archive_bytes + sum(e.size for e in live.values())

    for entry in entries:
        if _current_total() <= max_bytes:
            break
        if entry.path in live:
            _evict(entry, "mcp_cache_pruned_size")

    result.total_bytes_after = _current_total()
    if result.total_bytes_after > max_bytes:
        result.over_budget_residual = True
        trace.event(
            "MCP-CACHE",
            "mcp_cache_prune_over_budget residual=%d over max_bytes=%d (shared archive; "
            "run 'clio doctor --gc' for uv cache prune)",
            result.total_bytes_after,
            max_bytes,
        )
    return result


def boot_prune_mcp_cache(
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    others_alive: Callable[[], Iterable[Any]] | None = None,
    now: float | None = None,
) -> PruneResult | None:
    """Prune the MCP uv cache at server boot, IFF this process owns it (#1001).

    Boot is the one safe window: the cache is only unsafe to prune while a peer clio
    process might be spawning into it (astral-sh/uv#11694). This consults ``others_alive``
    and, if any peer is reported, SKIPS with a typed reason (never prunes mid-session).
    With no peers, it resolves the configured bounds and prunes.

    Args:
        cache_dir: The MCP uv cache dir; defaults to the canonical
            :func:`clio_agent.tools.mcp_config._mcp_uv_cache_dir`.
        others_alive: Zero-arg prober returning the live PEER clio processes (empty
            iterable = this process owns the cache). Defaults to
            :func:`clio_agent.runtime.disk_gc.live_peer_clio_processes`. Injected in tests.
        now: Reference epoch seconds (injectable for tests).

    Returns:
        The :class:`PruneResult`, or ``None`` when the prune was skipped because a peer is
        alive (a typed skip is logged either way).
    """
    if cache_dir is None:
        from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415

        cache_dir = _mcp_uv_cache_dir()
    if others_alive is None:
        from clio_agent.runtime.disk_gc import live_peer_clio_processes  # noqa: PLC0415

        others_alive = live_peer_clio_processes

    peers = list(others_alive())
    if peers:
        trace.event(
            "MCP-CACHE",
            "mcp_cache_prune_skipped_live peers=%d (%s) — boot prune deferred",
            len(peers),
            ", ".join(str(p) for p in peers[:8]),
        )
        return None

    bounds = cache_bounds()
    result = prune_uv_cache(
        cache_dir,
        max_bytes=bounds.max_bytes,
        max_age_days=bounds.max_age_days,
        now=now,
    )
    if result.existed:
        trace.event(
            "MCP-CACHE",
            "boot prune done dir=%s freed=%d evicted=%d before=%d after=%d",
            result.cache_dir,
            result.bytes_freed,
            result.count,
            result.total_bytes_before,
            result.total_bytes_after,
        )
    return result


async def boot_prune_off_loop() -> None:
    """Run :func:`boot_prune_mcp_cache` off the event loop (server-boot hook, #1001).

    A large-cache directory walk must never block uvicorn's port binding, so this runs the
    (synchronous) prune in the default executor. Best-effort: a failure is logged and
    swallowed so it can never break server boot. Typed prune reasons are emitted by
    :func:`boot_prune_mcp_cache` itself.
    """
    import asyncio  # noqa: PLC0415
    import logging  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, boot_prune_mcp_cache)
    except Exception:  # noqa: BLE001 - best-effort boot prune; a failure must never break server boot
        logging.getLogger(__name__).exception(
            "mcp-uv-cache boot prune failed (#1001); continuing boot"
        )
