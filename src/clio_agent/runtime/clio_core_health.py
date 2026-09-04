"""Doctor check: surface the effective clio-core CTE ram hot-tier capacity cap.

clio-core reads a ram tier ``capacity_limit`` of ``"0g"`` as *"default to 80% of total
system DRAM"* (#890) — so the value is normally implicit and invisible until the machine
is starved. This sub-check makes the number explicit in the doctor/health report: it
reads the ``cte.yaml`` the ARC backend will actually run with (without seeding it) and
emits one :class:`~clio_agent.runtime.status.IntegrationStatus` row reporting the
effective cap. A ``0g`` (= 80%-DRAM) ram tier — e.g. a stale config file generated
before the bounded default landed — is surfaced as a warning with the exact
remediation, rather than silently rewritten (see
:func:`clio_agent.arc.clio_core_config.default_cte_config_path`).

The row is emitted only when the ARC backend is the clio-core backend (``CLIO_ARC_STORE`` is
``cte`` or unset); for the explicit ``local`` backend the ram hot tier is irrelevant and
no row is produced (mirrors :meth:`RuntimeProbe._arc_backend`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.arc.clio_core_config import (
    RamTierCap,
    cte_disk_warn_fraction,
    cte_store_dir,
    effective_ram_cap,
    parse_capacity_bytes,
)
from clio_agent.runtime.humanize import format_bytes
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

if TYPE_CHECKING:
    from clio_agent.arc.clio_core_daemon import DaemonMemorySnapshot

_REMEDIATION = (
    "Set a bounded ram cap via arc.cte.ram_capacity (or env CLIO_ARC_CTE_RAM_CAPACITY), "
    "e.g. '2GB', then delete the stale cte.yaml so it regenerates — or edit the "
    "capacity_limit under storage[cte_ram_tier] in that file directly. Offload to the "
    "disk tier is byte-identical (tests/test_arc/test_clio_core_offload_spill.py), so a small "
    "hot tier is safe."
)


def _cap_source(cap: RamTierCap) -> str:
    """Return the config-source label for a ram-cap row."""
    if cap.file_exists:
        return f"file:{cap.config_path}"
    return "generator-default:arc.cte.ram_capacity"


def probe_clio_core_ram_cap(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Report the effective clio-core CTE ram hot-tier cap as a doctor row.

    Returns a single-row list when the ARC backend is clio-core, and an empty list for the
    explicit ``local`` backend (the ram tier does not apply). The row is:

    * MISCONFIGURED when the config file declares an unparseable cap (fail-loud);
    * DEGRADED when a PRESENT ram data tier is ``0g`` (= 80% of system DRAM — the
      #890 footgun) or the working-memory arena is unbounded (#906), with the
      exact remediation;
    * READY otherwise: the disk-only default (no ram data tier, arena bounded —
      the memory budget) or a bounded ram tier.

    Args:
        env: Environment mapping (defaults to the process environment). Drives the
            backend selection and the ``cte.yaml`` path resolution.

    Returns:
        Zero or one :class:`IntegrationStatus`.
    """
    import os  # noqa: PLC0415 - default env without a module-level os handle

    env = env if env is not None else os.environ
    backend = env.get("CLIO_ARC_STORE", "cte").strip().lower()
    if backend != "cte":
        return []

    cap = effective_ram_cap(env=env)
    source = _cap_source(cap)
    endpoint = cap.config_path
    details = {
        "config_path": cap.config_path,
        "config_exists": cap.file_exists,
        "ram_capacity_limit": cap.cap,
        "ram_bdev_capacity": cap.bdev_capacity,
        "cap_source": cap.source,
    }

    if cap.parse_error is not None:
        return [
            IntegrationStatus(
                name="clio_core_ram_cap",
                state=IntegrationState.MISCONFIGURED,
                summary=(
                    f"clio-core CTE ram hot-tier cap {cap.cap!r} is not a valid capacity: "
                    f"{cap.parse_error}."
                ),
                config_source=source,
                next_action=_REMEDIATION,
                endpoint=endpoint,
                details={**details, "reason": "ram_cap_unparseable"},
                required=True,
            )
        ]

    if cap.cap is None:
        # Disk-only data topology (the #906 desktop default): no ram data tier;
        # the memory bound is the working-memory arena (ram bdev capacity).
        arena = cap.bdev_capacity
        arena_bounded = False
        if arena is not None:
            try:
                arena_bounded = parse_capacity_bytes(arena) > 0
            except ValueError:
                arena_bounded = False
        if arena_bounded and arena is not None:
            human = format_bytes(parse_capacity_bytes(arena))
            return [
                IntegrationStatus(
                    name="clio_core_ram_cap",
                    state=IntegrationState.READY,
                    summary=(
                        f"clio-core runs disk-only data with working memory bounded at "
                        f"{arena} (~{human}); no ram data tier (#906 memory budget)."
                    ),
                    config_source=source,
                    next_action="No action required.",
                    endpoint=endpoint,
                    capabilities=["memory-budget", "disk-only-data"],
                    details={**details, "reason": "disk_only_arena_bounded"},
                    required=True,
                )
            ]
        return [
            IntegrationStatus(
                name="clio_core_ram_cap",
                state=IntegrationState.DEGRADED,
                summary=(
                    "clio-core CTE config has no ram data tier AND no bounded "
                    "working-memory arena — clio-core may grow to 80% of system "
                    "DRAM (#906)."
                ),
                config_source=source,
                next_action=_REMEDIATION,
                endpoint=endpoint,
                fallback="none",
                details={**details, "reason": "arena_unbounded"},
                required=True,
            )
        ]

    if cap.unbounded:
        return [
            IntegrationStatus(
                name="clio_core_ram_cap",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"clio-core CTE ram hot-tier cap is {cap.cap!r}, which clio-core reads "
                    "as 80% of total system DRAM — the context plane can consume most of "
                    "the machine (#890)."
                ),
                config_source=source,
                next_action=_REMEDIATION,
                endpoint=endpoint,
                fallback="none",
                details={**details, "reason": "ram_cap_unbounded_80pct_dram"},
                required=True,
            )
        ]

    human = format_bytes(parse_capacity_bytes(cap.cap))
    return [
        IntegrationStatus(
            name="clio_core_ram_cap",
            state=IntegrationState.READY,
            summary=(
                f"clio-core CTE ram hot tier is bounded at {cap.cap} (~{human}); cold blobs "
                "spill to the disk tier."
            ),
            config_source=source,
            next_action="No action required.",
            endpoint=endpoint,
            capabilities=["bounded-hot-tier", "disk-spill"],
            details={**details, "ram_capacity_bytes": parse_capacity_bytes(cap.cap)},
            required=True,
        )
    ]


def probe_clio_core_liveness(*, snapshot: list[dict] | None = None) -> list[IntegrationStatus]:
    """Surface a quarantined (daemon-lost) clio-core store as a doctor row (#892).

    Quarantine is per-process in-memory state on a live ``ClioCoreStore``'s liveness gate,
    not something a socket probe can observe — so this reads the process-local gate
    registry (:func:`clio_agent.arc.clio_core_liveness.liveness_snapshot`). It is meaningful
    only when the report runs IN the process that holds the store (e.g. the gact
    server's own status route); a separate doctor CLI holds no gate and correctly
    reports nothing.

    Returns:
        A single DEGRADED row when any gate is quarantined (a store wedged after a
        daemon loss, ops raising ``ClioCoreRuntimeLostError`` until the daemon returns); a
        single READY row when live gates exist and none is quarantined; and an empty
        list when this process holds no clio-core store (nothing to report).

    Args:
        snapshot: Optional injected gate snapshot (list of status dicts) for testing;
            defaults to the live process registry.
    """
    if snapshot is None:
        from clio_agent.arc.clio_core_liveness import liveness_snapshot  # noqa: PLC0415

        snapshot = liveness_snapshot()
    if not snapshot:
        return []

    quarantined = [gate for gate in snapshot if gate.get("quarantined")]
    if quarantined:
        gate = quarantined[0]
        port = gate.get("port")
        return [
            IntegrationStatus(
                name="clio_core_liveness",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"{len(quarantined)} of {len(snapshot)} clio-core store(s) are "
                    "QUARANTINED after a runtime-daemon loss; ARC ops raise "
                    "ClioCoreRuntimeLostError until the daemon returns (guards against the "
                    "clio-core#722 host access violation)."
                ),
                config_source="runtime:clio_core_liveness_gate",
                next_action=(
                    "Restart the shared clio-core daemon (clio start / clio_run start); "
                    "the store reconnects on the next ARC op. Or set CLIO_ARC_STORE=local."
                ),
                endpoint=None if port is None else f"127.0.0.1:{port}",
                fallback="none",
                details={
                    "reason": "clio_core_store_quarantined",
                    "quarantined_gates": len(quarantined),
                    "total_gates": len(snapshot),
                    "gate_reason": gate.get("reason", ""),
                },
                required=True,
            )
        ]
    return [
        IntegrationStatus(
            name="clio_core_liveness",
            state=IntegrationState.READY,
            summary=(
                f"{len(snapshot)} clio-core store liveness gate(s) active and healthy; "
                "ops are guarded against a runtime-daemon loss (#892)."
            ),
            config_source="runtime:clio_core_liveness_gate",
            next_action="No action required.",
            capabilities=["daemon-loss-guard"],
            details={"reason": "clio_core_liveness_healthy", "total_gates": len(snapshot)},
            required=True,
        )
    ]


def probe_clio_core_init_degradation(*, record: object | None = None) -> list[IntegrationStatus]:
    """Surface an INIT-time degrade from the clio-core backend to LocalFS as a row (#897).

    When ``make_arc_store`` cannot bring up the clio-core backend it degrades to
    :class:`~clio_agent.arc.storage.LocalFSStore` *loudly* and records a typed
    :class:`~clio_agent.arc.init_degradation.ArcInitDegradation` in a process-local
    slot. This reads that slot (mirroring the #892 gate registry: meaningful only IN
    the process that built the store — a separate doctor CLI holds none and reports
    nothing) and emits a DEGRADED row naming the cause and stating that the
    external-operator (clio-core) pathway is unavailable (#737).

    Args:
        record: Optional injected :class:`ArcInitDegradation` (or ``None``) for
            testing; defaults to the live process-local record.

    Returns:
        A single DEGRADED row when a degrade was recorded this process, else empty.
    """
    if record is None:
        from clio_agent.arc.init_degradation import arc_init_degradation_snapshot  # noqa: PLC0415

        record = arc_init_degradation_snapshot()
    if record is None:
        return []

    details = record.to_details()  # type: ignore[attr-defined]
    reason = details["reason"]
    selection = "explicit CLIO_ARC_STORE=cte" if details["was_explicit"] else "the default"
    return [
        IntegrationStatus(
            name="clio_core_init",
            state=IntegrationState.DEGRADED,
            summary=(
                "ARC degraded to LocalFSStore at init: the clio-core backend "
                f"({selection}) is UNAVAILABLE — the external-operator (clio-core) "
                f"pathway could not be brought up (reason={reason}: {details['error']}). "
                "ARC is running on local files; the tiered clio-core backend is not active."
            ),
            config_source="runtime:arc_init_degradation",
            next_action=(
                "Fix the clio-core install/config to restore the tiered backend (run "
                "clio doctor), or set CLIO_ARC_STORE=local to choose LocalFS "
                "deliberately (no degrade row). clio-core is retried on the next boot."
            ),
            endpoint=str(details["config_path"]) if details["config_path"] else None,
            fallback="local",
            details=details,
            required=True,
        )
    ]


def probe_clio_core_daemon_memory(
    *,
    env: Mapping[str, str] | None = None,
    snapshot: DaemonMemorySnapshot | None = None,
) -> list[IntegrationStatus]:
    """Surface the shared clio-core daemon's memory as a doctor row (#891).

    The shared ``clio_run`` daemon can grow *daemon-internally* — heap/arena/thread
    growth the #890 data-tier ram cap does not bound — and until now that growth was
    invisible (found only in Task Manager: 12.3 GiB resident / 20.2 GiB committed). This
    row makes it visible and bounded-by-policy:

    * READY when RSS is ``ok`` (below the elevated threshold);
    * DEGRADED with ``clio_core_daemon_rss_elevated`` when RSS is >= the warn threshold
      (default 1 GiB);
    * DEGRADED with ``clio_core_daemon_rss_critical`` when RSS is >= the critical
      threshold (default 4 GiB), naming the opt-in recycle policy in the remediation.

    Emitted only for the clio-core ARC backend (``CLIO_ARC_STORE`` is ``cte`` or unset)
    and only when a daemon is actually located (a down daemon is surfaced by the #892
    liveness row instead). The typed ``ok`` | ``elevated`` | ``critical`` status rides in
    ``details['daemon_mem_status']``; both non-ok statuses map to DEGRADED (the doctor
    state vocabulary has no separate elevated/critical rows).

    Args:
        env: Environment mapping (drives backend selection + port resolution); defaults
            to the process environment.
        snapshot: An injected :class:`~clio_agent.arc.clio_core_daemon.DaemonMemorySnapshot`
            (test seam); gathered fresh when ``None``.

    Returns:
        Zero or one :class:`IntegrationStatus`.
    """
    import os  # noqa: PLC0415 - default env without a module-level os handle

    from clio_agent.arc import clio_core_daemon  # noqa: PLC0415 - lazy: avoid load-time cycle

    env = env if env is not None else os.environ
    backend = env.get("CLIO_ARC_STORE", "cte").strip().lower()
    if backend != "cte":
        return []

    snap = (
        snapshot
        if snapshot is not None
        else clio_core_daemon.collect_daemon_memory_snapshot(env=env)
    )
    if snap is None:
        return []

    warn, critical = clio_core_daemon._resolve_daemon_rss_thresholds()
    status = clio_core_daemon.classify_daemon_rss(snap.rss_bytes, warn=warn, critical=critical)
    human_rss = format_bytes(snap.rss_bytes)
    human_committed = format_bytes(snap.committed_bytes)
    details = {
        **snap.to_details(),
        "daemon_mem_status": status,
        "rss_warn_bytes": warn,
        "rss_critical_bytes": critical,
    }
    endpoint = f"127.0.0.1:{snap.port}"
    clients = f"{snap.live_client_count} live client(s)" + (
        f", {snap.stale_client_count} stale" if snap.stale_client_count else ""
    )

    if status == "ok":
        return [
            IntegrationStatus(
                name="clio_core_daemon_memory",
                state=IntegrationState.READY,
                summary=(
                    f"clio-core daemon (pid {snap.pid}) RSS is {human_rss} "
                    f"(committed {human_committed}), {snap.thread_count} threads, "
                    f"{clients}; within the elevated threshold (~{format_bytes(warn)})."
                ),
                config_source="runtime:clio_core_daemon_memory",
                next_action="No action required.",
                endpoint=endpoint,
                capabilities=["daemon-memory-visible"],
                details={**details, "reason": "clio_core_daemon_rss_ok"},
                required=True,
            )
        ]

    reason = (
        "clio_core_daemon_rss_critical" if status == "critical" else "clio_core_daemon_rss_elevated"
    )
    threshold = format_bytes(critical if status == "critical" else warn)
    next_action = (
        "The shared clio-core daemon has grown daemon-internally (upstream-filed; the "
        "#890 data-tier cap does not bound it). Restart it with no live clients (clio "
        "restart / clio_run stop), or enable the opt-in bounded recycle "
        "(CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE=1 / arc.clio_core.daemon_recycle_enabled) to "
        "auto-recycle it when critical AND idle."
    )
    return [
        IntegrationStatus(
            name="clio_core_daemon_memory",
            state=IntegrationState.DEGRADED,
            summary=(
                f"clio-core daemon (pid {snap.pid}) RSS is {human_rss} "
                f"(committed {human_committed}) — {status.upper()}, over the "
                f"{status} threshold (~{threshold}); {snap.thread_count} threads, {clients} (#891)."
            ),
            config_source="runtime:clio_core_daemon_memory",
            next_action=next_action,
            endpoint=endpoint,
            fallback="none",
            details={**details, "reason": reason},
            required=True,
        )
    ]


def _cte_cold_tier_dir_usage(store_dir: Path) -> tuple[int | None, int]:
    """Return measurable allocated bytes and logical bytes for the CTE cold tier.

    clio-core preallocates its file tier as a sparse file. ``st_size`` therefore
    describes the tier's logical capacity, not the disk space consumed by data.
    POSIX ``st_blocks`` reports the allocated 512-byte blocks and lets the health
    probe distinguish a fresh sparse tier from one that is genuinely filling up.
    Platforms without ``st_blocks`` return ``None`` for allocation. In particular,
    Windows reports a clio-core preallocated backing file at its configured logical
    capacity even when almost no CTE data has been written. Treating that capacity as
    current usage creates a permanent false 100% warning.
    """
    allocated_total = 0
    allocation_available = True
    logical_total = 0
    for root, _dirs, files in os.walk(store_dir, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                stat = os.lstat(fp)
            except OSError:
                continue
            logical_total += stat.st_size
            blocks = getattr(stat, "st_blocks", None)
            if blocks is None:
                allocation_available = False
            else:
                allocated_total += int(blocks) * 512
    return (allocated_total if allocation_available else None), logical_total


def probe_cte_cold_tier_disk(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Warn when the CTE cold (file) tier exceeds a fraction of its capacity (#1001).

    Visibility first: the CTE file tier has a large capacity bound (default 50GB,
    ``arc.cte.file_capacity``) but clio-core does not yet trim it (upstream-gated). This
    doctor row measures the cold-tier data directory and WARNS (DEGRADED) when it exceeds
    :func:`cte_disk_warn_fraction` of that capacity, so the operator sees the growth long
    before writes fail. Actual trimming is upstream (clio-core); this is the demand-side
    visibility clio-agent can ship today.

    Emitted only for the clio-core backend (``CLIO_ARC_STORE`` ``cte`` or unset).
    """
    env = env if env is not None else os.environ
    backend = env.get("CLIO_ARC_STORE", "cte").strip().lower()
    if backend != "cte":
        return []

    cap = effective_ram_cap(env=env)
    if cap.final_tier_capacity is None:
        return []
    try:
        capacity_bytes = parse_capacity_bytes(cap.final_tier_capacity)
    except ValueError:
        return []
    if capacity_bytes <= 0:
        return []

    store_dir = cte_store_dir(env=env, config_path=cap.config_path)
    allocated, logical = _cte_cold_tier_dir_usage(store_dir) if store_dir.is_dir() else (0, 0)
    warn_fraction = cte_disk_warn_fraction()
    fraction = allocated / capacity_bytes if allocated is not None and capacity_bytes else None
    details = {
        "store_dir": str(store_dir),
        # ``None`` is intentional: logical capacity is not current usage.
        "used_bytes": allocated,
        "allocated_bytes": allocated,
        "logical_bytes": logical,
        "capacity_bytes": capacity_bytes,
        "capacity": cap.final_tier_capacity,
        "used_fraction": round(fraction, 4) if fraction is not None else None,
        "warn_fraction": warn_fraction,
        "trim_status": "upstream_gated",
        "allocation_measurement": "physical_blocks" if allocated is not None else "unavailable",
    }
    if fraction is None:
        return [
            IntegrationStatus(
                name="cte_cold_tier_disk",
                state=IntegrationState.SKIPPED,
                summary=(
                    f"CTE cold tier capacity is {cap.final_tier_capacity}. Its backing files "
                    f"have {format_bytes(logical)} logical size; that is reserved capacity, "
                    "not current data usage. Physical occupancy is not exposed on this platform."
                ),
                config_source="arc.cte.file_capacity, runtime:filesystem-allocation",
                next_action=(
                    "No action required. Report in-pool occupancy when clio-core exposes that "
                    "metric; do not infer it from the preallocated backing-file size."
                ),
                details=details,
                required=False,
            )
        ]
    assert allocated is not None  # narrowed with ``fraction`` for static checkers
    if fraction >= warn_fraction:
        return [
            IntegrationStatus(
                name="cte_cold_tier_disk",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"CTE cold tier has {format_bytes(allocated)} allocated "
                    f"({fraction:.0%} of its {cap.final_tier_capacity} capacity; "
                    f"warn at {warn_fraction:.0%}). "
                    f"Its sparse files have {format_bytes(logical)} logical size. "
                    "clio-core does not trim the cold tier yet (upstream-gated)."
                ),
                config_source="arc.cte.file_capacity, arc.cte.disk_warn_fraction",
                next_action=(
                    "Reclaim space (archive/remove old workspace CTE data) or raise "
                    "arc.cte.file_capacity. Automatic cold-tier trim is tracked upstream "
                    "(clio-core)."
                ),
                fallback="cold-tier-grows-until-writes-fail (PutBlob rc=13)",
                details=details,
                required=False,
            )
        ]
    return [
        IntegrationStatus(
            name="cte_cold_tier_disk",
            state=IntegrationState.READY,
            summary=(
                f"CTE cold tier has {format_bytes(allocated)} allocated "
                f"({fraction:.0%} of its {cap.final_tier_capacity} capacity; "
                f"warn at {warn_fraction:.0%}). Its sparse files have "
                f"{format_bytes(logical)} logical size."
            ),
            config_source="arc.cte.file_capacity, arc.cte.disk_warn_fraction",
            next_action="No action required.",
            details=details,
            required=False,
        )
    ]


def probe_clio_core_write_health(
    *, env: Mapping[str, str] | None = None
) -> list[IntegrationStatus]:
    """Fail health while clio-core is currently losing required writes.

    Live state, never a latch: the row is present only while a write was lost
    and NO write has succeeded since (see
    :func:`clio_agent.arc.clio_core_retry.last_lost_put_write`), so a recovered
    backend stops reporting hard-down on its next successful write rather than
    for the rest of the process lifetime.

    Args:
        env: Environment mapping; defaults to the process environment.

    Returns:
        One required ``clio_core_write`` row, or an empty list.
    """
    env = env if env is not None else os.environ
    if env.get("CLIO_ARC_STORE", "cte").strip().lower() != "cte":
        return []

    from clio_agent.arc.clio_core_retry import last_lost_put_write  # noqa: PLC0415

    failure = last_lost_put_write()
    if failure is None:
        return []
    details = failure.to_details()
    return [
        IntegrationStatus(
            name="clio_core_write",
            state=IntegrationState.UNAVAILABLE,
            summary=(
                "clio-core rejected a required persistent write and none has "
                f"succeeded since; new transcript state is not durable ({details['error']})."
            ),
            config_source="runtime:clio_core_put_failure",
            next_action=(
                "Repair or expand the active clio-core tier. This row clears itself "
                "on the next successful write — no backend restart is required."
            ),
            fallback="none",
            details=details,
            required=True,
        )
    ]


def probe_clio_core_health(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Aggregate the clio-core doctor rows: init (#897) + ram cap (#890) + liveness (#892) + daemon mem (#891) + cold-tier disk (#1001).

    A single collection seam so the doctor wires ONE call for all clio-core sub-checks.

    Args:
        env: Environment mapping forwarded to the sub-probes.

    Returns:
        The concatenated init-degradation, ram-cap, liveness, daemon-memory, and
        cold-tier-disk rows (each may be empty).
    """
    return [
        *probe_clio_core_init_degradation(),
        *probe_clio_core_ram_cap(env=env),
        *probe_clio_core_liveness(),
        *probe_clio_core_daemon_memory(env=env),
        *probe_cte_cold_tier_disk(env=env),
        *probe_clio_core_write_health(env=env),
    ]
