"""Doctor rows for the CTE cold tier's files: data occupancy and the metadata log.

The cold-tier directory holds two things that must not be added together:

* the **data file** (``storage.bin_node0``). iowarp-core 2.3.2 allocates it at its full
  capacity on Linux before the first write, so its allocated blocks equal its capacity
  and say nothing about how much of it holds data. Only a sparse data file (allocated
  less than its logical size) or one that grows as it is written can be measured from
  the filesystem.
* the **metadata log** (``metadata.log*``): a write-ahead log with one record per blob
  update, each carrying the blob's whole block list. clio-core truncates it only after a
  full metadata snapshot completes (normally once it passes ``transaction_log_capacity``);
  when snapshots stop completing it grows without bound. The tier's capacity does not
  bound it, and the daemon replays it into memory at every start (measured: a 2.2 GB log
  put a freshly restarted daemon at 2.3 GB resident; a 110 MB log at 141 MB).

Adding them made a fully preallocated 2 GB tier plus a 2.2 GB log read as "203% of its
capacity", with a remediation (raise the capacity) that could not help. Each is now its
own row with its own reason (#1001).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clio_agent.arc.clio_core_config import (
    cte_disk_warn_fraction,
    cte_store_dir,
    effective_ram_cap,
    parse_capacity_bytes,
)
from clio_agent.runtime.humanize import format_bytes
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

__all__ = [
    "ColdTierUsage",
    "cold_tier_usage",
    "metadata_log_bytes",
    "metadata_log_warn_bytes",
    "probe_cte_cold_tier_disk",
    "probe_cte_metadata_log",
]

_METADATA_LOG_PREFIX = "metadata.log"


@dataclass(frozen=True)
class ColdTierUsage:
    """Filesystem measurements of one cold-tier directory."""

    data_allocated: int | None  # ``None`` where the platform has no ``st_blocks``
    data_logical: int
    metadata_log_bytes: int

    def data_preallocated(self, capacity_bytes: int) -> bool:
        """Whether the data files are allocated at (at least) the tier's full capacity."""
        return (
            self.data_allocated is not None
            and self.data_logical >= capacity_bytes
            and self.data_allocated >= self.data_logical
        )


def cold_tier_usage(store_dir: Path) -> ColdTierUsage:
    """Measure the data files and the metadata log under ``store_dir`` separately.

    POSIX ``st_blocks`` gives a file's allocated bytes, which tells a sparse data file
    from one holding data; platforms without it report ``data_allocated=None``. The
    metadata log is append-only and never sparse, so its logical size is its size.
    """
    allocated = 0
    allocation_available = True
    logical = 0
    metadata_log = 0
    for root, _dirs, files in os.walk(store_dir, followlinks=False):
        for name in files:
            try:
                stat = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if name.startswith(_METADATA_LOG_PREFIX):
                metadata_log += stat.st_size
                continue
            logical += stat.st_size
            blocks = getattr(stat, "st_blocks", None)
            if blocks is None:
                allocation_available = False
            else:
                allocated += int(blocks) * 512
    return ColdTierUsage(allocated if allocation_available else None, logical, metadata_log)


def _measure(env: Mapping[str, str]) -> tuple[str, int, Path, ColdTierUsage] | None:
    """The configured capacity, its bytes, the store directory and its usage."""
    cap = effective_ram_cap(env=env)
    if cap.final_tier_capacity is None:
        return None
    try:
        capacity_bytes = parse_capacity_bytes(cap.final_tier_capacity)
    except ValueError:
        return None
    if capacity_bytes <= 0:
        return None
    store_dir = cte_store_dir(env=env, config_path=cap.config_path)
    usage = cold_tier_usage(store_dir) if store_dir.is_dir() else ColdTierUsage(0, 0, 0)
    return cap.final_tier_capacity, capacity_bytes, store_dir, usage


def probe_cte_cold_tier_disk(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Warn when the cold tier's data files fill past a fraction of its capacity (#1001).

    Counts the data files only (the metadata log is :func:`probe_cte_metadata_log`). A
    data file allocated at the full capacity is preallocation, not occupancy, so it is
    reported as unmeasurable rather than as a permanent 100% warning.
    """
    env = env if env is not None else os.environ
    measured = _measure(env)
    if measured is None:
        return []
    capacity, capacity_bytes, store_dir, usage = measured
    allocated = usage.data_allocated
    preallocated = usage.data_preallocated(capacity_bytes)
    measurable = allocated is not None and not preallocated
    fraction = allocated / capacity_bytes if measurable and allocated is not None else None
    warn_fraction = cte_disk_warn_fraction()
    details = {
        "store_dir": str(store_dir),
        # ``None`` is intentional: reserved capacity is not current usage.
        "used_bytes": allocated if measurable else None,
        "allocated_bytes": allocated,
        "logical_bytes": usage.data_logical,
        "metadata_log_bytes": usage.metadata_log_bytes,
        "capacity_bytes": capacity_bytes,
        "capacity": capacity,
        "used_fraction": round(fraction, 4) if fraction is not None else None,
        "warn_fraction": warn_fraction,
        "trim_status": "upstream_gated",
        "allocation_measurement": (
            "preallocated"
            if preallocated
            else "physical_blocks"
            if allocated is not None
            else "unavailable"
        ),
    }
    if fraction is None:
        why = (
            "its data file is allocated at the full capacity before any data is written"
            if preallocated
            else "physical occupancy is not exposed on this platform"
        )
        return [
            IntegrationStatus(
                name="cte_cold_tier_disk",
                state=IntegrationState.SKIPPED,
                summary=(
                    f"CTE cold tier capacity is {capacity}. Its data files have "
                    f"{format_bytes(usage.data_logical)} logical size; that is reserved "
                    f"capacity, not current data usage ({why})."
                ),
                config_source="arc.cte.file_capacity, runtime:filesystem-allocation",
                next_action=(
                    "No action required. Report in-pool occupancy when clio-core exposes that "
                    "metric; do not infer it from the preallocated backing-file size."
                ),
                details={**details, "reason": "cte_cold_tier_occupancy_unmeasurable"},
                required=False,
            )
        ]
    summary = (
        f"CTE cold tier data files have {format_bytes(allocated or 0)} allocated "
        f"({fraction:.0%} of its {capacity} capacity; warn at {warn_fraction:.0%}). "
        f"Their logical size is {format_bytes(usage.data_logical)}."
    )
    if fraction >= warn_fraction:
        return [
            IntegrationStatus(
                name="cte_cold_tier_disk",
                state=IntegrationState.DEGRADED,
                summary=f"{summary} clio-core does not trim the cold tier yet (upstream-gated).",
                config_source="arc.cte.file_capacity, arc.cte.disk_warn_fraction",
                next_action=(
                    "Reclaim space (archive/remove old workspace CTE data) or raise "
                    "arc.cte.file_capacity. Automatic cold-tier trim is tracked upstream "
                    "(clio-core)."
                ),
                fallback="cold-tier-grows-until-writes-fail (PutBlob rc=13)",
                details={**details, "reason": "cte_cold_tier_over_fraction"},
                required=False,
            )
        ]
    return [
        IntegrationStatus(
            name="cte_cold_tier_disk",
            state=IntegrationState.READY,
            summary=summary,
            config_source="arc.cte.file_capacity, arc.cte.disk_warn_fraction",
            next_action="No action required.",
            details={**details, "reason": "cte_cold_tier_ok"},
            required=False,
        )
    ]


def metadata_log_bytes(env: Mapping[str, str]) -> int | None:
    """Size of the configured store's metadata log, or ``None`` with no bounded file tier."""
    measured = _measure(env)
    return None if measured is None else measured[3].metadata_log_bytes


def metadata_log_warn_bytes() -> int:
    """The log size that warns: the daemon-memory warn threshold, since a start replays it."""
    from clio_agent.arc import clio_core_daemon  # noqa: PLC0415 - lazy: avoid load-time cycle

    return clio_core_daemon._resolve_daemon_rss_thresholds()[0]


def probe_cte_metadata_log(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Report the size of clio-core's metadata write-ahead log.

    clio-core truncates the log after each completed metadata snapshot, so it normally
    stays near ``transaction_log_capacity``. DEGRADED (``cte_metadata_log_large``) once it
    reaches the daemon-memory warn threshold: snapshots are then not completing, and the
    daemon replays the whole log into memory at start, so a restart cannot bring the
    daemon's memory under that threshold.
    """
    env = env if env is not None else os.environ
    measured = _measure(env)
    if measured is None:
        return []
    _capacity, _capacity_bytes, store_dir, usage = measured
    size = usage.metadata_log_bytes
    warn = metadata_log_warn_bytes()
    details = {"store_dir": str(store_dir), "metadata_log_bytes": size, "warn_bytes": warn}
    if size < warn:
        return [
            IntegrationStatus(
                name="cte_metadata_log",
                state=IntegrationState.READY,
                summary=(
                    f"clio-core metadata log is {format_bytes(size)} "
                    f"(warn at {format_bytes(warn)})."
                ),
                config_source="runtime:filesystem-size",
                next_action="No action required.",
                details={**details, "reason": "cte_metadata_log_ok"},
                required=False,
            )
        ]
    return [
        IntegrationStatus(
            name="cte_metadata_log",
            state=IntegrationState.DEGRADED,
            summary=(
                f"clio-core metadata log is {format_bytes(size)} (warn at "
                f"{format_bytes(warn)}): clio-core's metadata snapshots are not completing, so "
                "the log is never truncated, and a daemon start replays all of it into "
                "memory, so restarting the daemon does not reduce its memory."
            ),
            config_source="runtime:filesystem-size",
            next_action=(
                "Report the stalled metadata snapshot upstream (clio-core; the partial "
                "snapshot is metadata.log.tmp beside the log). CLIO's size-classed records "
                "grow the log about 12 times more slowly, but the existing log only shrinks "
                "when a snapshot completes or the store directory is moved aside, which "
                "discards its ARC data."
            ),
            fallback="metadata-log-grows-without-bound",
            details={**details, "reason": "cte_metadata_log_large"},
            required=False,
        )
    ]
