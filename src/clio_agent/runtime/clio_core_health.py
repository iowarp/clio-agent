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

clio-core is the only ARC store, so the row is always emitted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.arc.clio_core_attach import ClioCoreAttachPhase, attach_state_snapshot
from clio_agent.arc.clio_core_config import RamTierCap, effective_ram_cap, parse_capacity_bytes
from clio_agent.arc.clio_core_liveness import (
    SOCKET_PROBE_UNAVAILABLE_REASON,
    runtime_socket_status,
)
from clio_agent.arc.runtime_crash import read_crash_record, summarize_crash
from clio_agent.runtime.cte_cold_tier import (
    metadata_log_bytes,
    probe_cte_cold_tier_disk,
    probe_cte_metadata_log,
)
from clio_agent.runtime.cte_store_location import probe_cte_store_location
from clio_agent.runtime.humanize import format_bytes
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

if TYPE_CHECKING:
    from clio_agent.arc.clio_core_daemon import DaemonMemorySnapshot
    from clio_agent.arc.history_mode import ContextMode

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

    Returns a single-row list (clio-core is the only ARC store). The row is:

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


def socket_probe_unavailable_row(
    name: str, *, source: str, port: int, details: dict[str, object]
) -> IntegrationStatus:
    """Expose a local diagnostic failure without prescribing daemon replacement."""
    return IntegrationStatus(
        name=name,
        state=IntegrationState.DEGRADED,
        summary=f"The local socket check could not establish Core availability on port {port}.",
        config_source=source,
        next_action="Retry the operation; an existing client can verify its original Core process.",
        endpoint=f"127.0.0.1:{port}",
        fallback="none",
        details={**details, "reason": SOCKET_PROBE_UNAVAILABLE_REASON},
        required=True,
    )


def daemon_not_listening_row(
    name: str, *, source: str, endpoint: str, details: dict[str, object], port: int, log_path: str
) -> IntegrationStatus:
    """The shared clio-core daemon is not listening: down only when that is a failure.

    CLIO starts the daemon itself on first use (and the server at boot), so a daemon that
    is not up yet is DEGRADED ``clio_core_starting`` -- a fresh server answered 503 before
    its first agent build. It is UNAVAILABLE when this process's attach failed, when an
    attached daemon is gone, or when the daemon left a crash record.
    """
    if details.get("reason") == SOCKET_PROBE_UNAVAILABLE_REASON:
        return socket_probe_unavailable_row(name, source=source, port=port, details=details)
    snap = attach_state_snapshot()
    crash = read_crash_record(Path(log_path).parent)
    lost = snap.phase in (ClioCoreAttachPhase.UNAVAILABLE, ClioCoreAttachPhase.ATTACHED)
    if lost or crash is not None:
        why = summarize_crash(crash) if crash is not None else (snap.error or snap.reason)
        return IntegrationStatus(
            name=name,
            state=IntegrationState.UNAVAILABLE,
            summary=f"The shared clio-core daemon is not listening on port {port}: {why}",
            config_source=source,
            next_action=f"Restart CLIO (it starts clio-core); see {log_path}.",
            endpoint=endpoint,
            fallback="none",
            details=details,
            required=True,
        )
    return IntegrationStatus(
        name=name,
        state=IntegrationState.DEGRADED,
        summary=(
            f"clio-core is not running yet on port {port}; CLIO starts it on first use "
            "(or it is starting now)."
        ),
        config_source=source,
        next_action="No action needed; this row turns ready once clio-core listens.",
        endpoint=endpoint,
        fallback="none",
        details={**details, "reason": "clio_core_starting"},
        required=True,
    )


def history_mode_row(
    name: str, mode: "ContextMode", source: str, endpoint: str, details: dict[str, object]
) -> IntegrationStatus:
    """The ``arc`` / ``clio_core`` row in the loud History mode: DEGRADED (a working
    server, never a 503; no daemon is expected), with the mode, its reason and the remedy."""
    return IntegrationStatus(
        name=name,
        state=IntegrationState.DEGRADED,
        summary=(
            "clio-core is not installed on this platform, so CLIO runs in History mode: the "
            "agent's context is held in memory only (nothing durable; context edits, "
            "compaction and search are unavailable)."
        ),
        config_source=source,
        next_action="Install a build of iowarp-core for this platform, then restart CLIO.",
        endpoint=endpoint,
        fallback="history",
        details={**details, **mode.as_dict()},
        required=True,
    )


def probe_clio_core_attach(*, state: object | None = None) -> list[IntegrationStatus]:
    """Surface this process's clio-core attach progress as the ``clio_core_attach`` row.

    ARC construction (connect-or-spawn + native attach) runs off the server's event
    loop, so ``/v1/health`` answers while it is still in flight; this row says where it
    is: ``starting`` (DEGRADED, never 503 by itself), ``attached`` (READY), or
    ``unavailable`` with the typed init reason (DEGRADED; the agent stays unready, typed).
    Process-local like the #892 gate registry: a separate doctor CLI reports nothing.
    History mode attempts no attach (no row); the ``arc`` row reports it.

    Args:
        state: Optional injected :class:`~clio_agent.arc.clio_core_attach.ClioCoreAttachState`
            for testing; defaults to the live process record.

    Returns:
        One row, or empty when no attach was attempted (idle, or History mode).
    """
    from clio_agent.arc.clio_core_attach import (  # noqa: PLC0415 - keep import light
        ClioCoreAttachPhase,
        ClioCoreAttachState,
        attach_state_snapshot,
    )
    from clio_agent.arc.clio_core_native_preflight import (  # noqa: PLC0415
        CLIO_CORE_EMBEDDED_RUNTIME_ENV_REMOVED,
        removed_embedded_runtime_env,
    )

    live_snapshot = state is None
    snap = state if isinstance(state, ClioCoreAttachState) else attach_state_snapshot()
    if snap.phase is ClioCoreAttachPhase.IDLE:
        return []
    details = snap.to_details()
    removed = removed_embedded_runtime_env()
    if removed:  # an inherited CLIO_WITH_RUNTIME was dropped before the attach
        details["removed_env"] = removed
        details["removed_env_reason"] = CLIO_CORE_EMBEDDED_RUNTIME_ENV_REMOVED
    endpoint = None if snap.port is None else f"127.0.0.1:{snap.port}"
    where = f"port {snap.port}, config {snap.config_path or '<default>'}"
    if snap.phase is ClioCoreAttachPhase.STARTING:
        state_, required = IntegrationState.DEGRADED, False
        summary = f"clio-core attach in progress ({where}); ARC is not live yet."
        next_action = "Wait; this row turns ready (attached) or names the failure."
    elif snap.phase is ClioCoreAttachPhase.ATTACHED:
        alive, probe_reason = (
            runtime_socket_status(snap.port)
            if live_snapshot and snap.port is not None
            else (True, None)
        )
        if probe_reason and snap.port is not None:
            return [
                socket_probe_unavailable_row(
                    "clio_core_attach",
                    source="runtime:clio_core_attach",
                    port=snap.port,
                    details=details,
                )
            ]
        if not alive:
            state_, required = IntegrationState.DEGRADED, True
            details["reason"] = "clio_core_daemon_lost_after_attach"
            summary = f"clio-core was attached, but its daemon is no longer listening ({where})."
            next_action = "Restart the clio-core daemon; retry the affected ARC operation."
        else:
            state_, required = IntegrationState.READY, False
            summary = f"clio-core attached ({where})."
            next_action = "No action required."
    else:
        state_, required = IntegrationState.DEGRADED, True
        summary = f"clio-core unavailable (reason={snap.reason}, {where}): {snap.error}"
        next_action = "Run clio doctor; see the clio_core_init row and the daemon log."
    return [
        IntegrationStatus(
            name="clio_core_attach",
            state=state_,
            summary=summary,
            config_source="runtime:clio_core_attach",
            next_action=next_action,
            endpoint=endpoint,
            fallback="none",
            details=details,
            required=required,
        )
    ]


def probe_clio_core_config_adoption(*, record: object | None = None) -> list[IntegrationStatus]:
    """Surface a first-config-wins adoption as the ``clio_core_config_adoption`` row.

    One clio-core daemon runs per machine. When this process asked for a different
    config than the running daemon's, it attached with the daemon's config
    (:func:`clio_agent.arc.clio_core_daemon_version.resolve_effective_config`); this row
    names the settings that differ so the substitution is never silent. DEGRADED (this
    CLIO's own settings are not in effect) but not required, so it never trips a 503.

    Args:
        record: Optional injected ``ConfigAdoption`` for testing; defaults to the live
            process record.

    Returns:
        One row when this process adopted the daemon's config, else empty.
    """
    from clio_agent.arc.clio_core_daemon_version import (  # noqa: PLC0415 - keep import light
        CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON,
        ConfigAdoption,
        config_adoption_snapshot,
    )

    snap = record if isinstance(record, ConfigAdoption) else config_adoption_snapshot()
    if snap is None:
        return []
    keys = ", ".join(sorted(snap.diffs)) or "unspecified keys"
    return [
        IntegrationStatus(
            name="clio_core_config_adoption",
            state=IntegrationState.DEGRADED,
            summary=(
                f"clio-core is running another CLIO's config ({snap.effective_config_path}); "
                f"this CLIO asked for {snap.requested_config_path}, which differs in: {keys}. "
                "The first config wins on the one daemon per machine."
            ),
            config_source="runtime:clio_core_config_adoption",
            next_action=(
                "No action needed to keep running. To apply this CLIO's settings, stop "
                "every CLIO on this machine so the daemon exits, then start this one first."
            ),
            endpoint=snap.effective_config_path,
            fallback="none",
            details={
                "reason": CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON,
                "requested_config_path": snap.requested_config_path,
                "effective_config_path": snap.effective_config_path,
                "diffs": snap.diffs,
            },
            required=False,
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
        daemon loss, ops raising ``ClioCoreRuntimeLostError`` until recovery); a
        single READY row when live gates exist and none is quarantined; and an empty
        list when this process holds no clio-core store (nothing to report).

    Args:
        snapshot: Optional injected gate snapshot (list of status dicts) for testing;
            defaults to the live process registry.
    """
    live_snapshot = snapshot is None
    if snapshot is None:
        from clio_agent.arc.clio_core_liveness import liveness_snapshot  # noqa: PLC0415

        snapshot = liveness_snapshot()
    if not snapshot:
        return []

    quarantined = [gate for gate in snapshot if gate.get("quarantined")]
    if quarantined:
        gate = quarantined[0]
        port = gate.get("port")
        restart_required = any(item.get("restart_required") for item in quarantined)
        if (
            gate.get("reason") == SOCKET_PROBE_UNAVAILABLE_REASON
            and not restart_required
            and isinstance(port, int)
        ):
            return [
                socket_probe_unavailable_row(
                    "clio_core_liveness",
                    source="runtime:clio_core_liveness_gate",
                    port=port,
                    details={"quarantined_gates": len(quarantined), "total_gates": len(snapshot)},
                )
            ]
        recovery_summary = (
            "ClioCoreRuntimeLostError until the agent restarts."
            if restart_required
            else "ClioCoreRuntimeLostError until the daemon returns (guards against the "
            "clio-core#722 host access violation)."
        )
        next_action = (
            "Restart the CLIO agent to establish a fresh native clio-core client."
            if restart_required
            else "Restart the shared clio-core daemon (clio start / clio_run start); "
            "the store reconnects on the next ARC op."
        )
        return [
            IntegrationStatus(
                name="clio_core_liveness",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"{len(quarantined)} of {len(snapshot)} clio-core store(s) are "
                    f"QUARANTINED after a runtime-daemon loss; ARC ops raise {recovery_summary}"
                ),
                config_source="runtime:clio_core_liveness_gate",
                next_action=next_action,
                endpoint=None if port is None else f"127.0.0.1:{port}",
                fallback="none",
                details={
                    "reason": "clio_core_store_quarantined",
                    "quarantined_gates": len(quarantined),
                    "total_gates": len(snapshot),
                    "gate_reason": gate.get("reason", ""),
                    "restart_required": restart_required,
                },
                required=True,
            )
        ]
    if live_snapshot:
        missing = []
        for gate in snapshot:
            port = gate.get("port")
            if not isinstance(port, int):
                continue
            alive, probe_reason = runtime_socket_status(port)
            if probe_reason:
                return [
                    socket_probe_unavailable_row(
                        "clio_core_liveness",
                        source="runtime:clio_core_liveness_gate",
                        port=port,
                        details={"total_gates": len(snapshot)},
                    )
                ]
            if not alive:
                missing.append(gate)
        if missing:
            port = missing[0]["port"]
            return [
                IntegrationStatus(
                    name="clio_core_liveness",
                    state=IntegrationState.DEGRADED,
                    summary=f"{len(missing)} of {len(snapshot)} clio-core daemon endpoint(s) are not listening; ARC operations may fail until the daemon returns.",
                    config_source="runtime:clio_core_liveness_gate",
                    next_action="Restart the clio-core daemon; retry the affected ARC operation.",
                    endpoint=f"127.0.0.1:{port}",
                    fallback="none",
                    details={
                        "reason": "clio_core_daemon_lost",
                        "missing_gates": len(missing),
                        "total_gates": len(snapshot),
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

    Emitted only when a daemon is actually located (a down daemon is surfaced by the #892
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
    replayed = metadata_log_bytes(env)
    details["metadata_log_bytes"] = replayed
    replay_note = (
        f"Its metadata log is {format_bytes(replayed)} and a daemon start replays all of it "
        "into memory, so a restart will not bring it under the threshold (cte_metadata_log). "
        if replayed is not None and replayed >= warn
        else ""
    )
    next_action = replay_note + (
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


def probe_clio_core_search_health(
    *, env: Mapping[str, str] | None = None
) -> list[IntegrationStatus]:
    """Fail health when the resolved clio-core config cannot search (clio-core#905).

    Long-term memory search and ``recall_context`` both need the ``clio_cte_indexer``
    chimod; without it every search is refused with the typed reason
    ``clio_core_search_unavailable``.

    Args:
        env: Environment mapping driving the config resolution.

    Returns:
        One required ``clio_core_search`` row when an existing config cannot search,
        else none. A config not written yet is not a verdict: CLIO seeds its own with
        the indexer on first attach, and the clio-core rows report the cold start.
    """
    from clio_agent.arc import clio_core_indexer as indexer  # noqa: PLC0415
    from clio_agent.arc.clio_core_config import _resolve_config_path  # noqa: PLC0415

    env = env if env is not None else os.environ
    path = _resolve_config_path(env)
    if not path or not Path(path).expanduser().is_file():
        return []
    if indexer.indexer_chimod_present(env=env, config_path=path):
        return []
    return [
        IntegrationStatus(
            name="clio_core_search",
            state=IntegrationState.UNAVAILABLE,
            summary=(
                "clio-core cannot search: the config composes no clio_cte_indexer chimod, so "
                "long-term memory search and context recall are unavailable."
            ),
            config_source="clio-core config",
            next_action=indexer.SEARCH_REMEDY[0].upper() + indexer.SEARCH_REMEDY[1:] + ".",
            fallback="none",
            details={
                "reason": indexer.CLIO_CORE_SEARCH_UNAVAILABLE,
                "upstream": indexer.UPSTREAM_INDEXER_ISSUE,
            },
            required=True,
        )
    ]


def probe_clio_core_health(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Aggregate the clio-core doctor rows: attach + config adoption + init (#897) + ram cap (#890) + liveness (#892) + daemon mem (#891) + cold-tier disk (#1001).

    A single collection seam so the doctor wires ONE call for all clio-core sub-checks.

    Args:
        env: Environment mapping forwarded to the sub-probes.

    Returns:
        The concatenated init-degradation, ram-cap, liveness, daemon-memory, and
        cold-tier-disk rows (each may be empty).
    """
    return [
        *probe_clio_core_attach(),
        *probe_clio_core_config_adoption(),
        *probe_cte_store_location(env=env),
        *probe_clio_core_ram_cap(env=env),
        *probe_clio_core_liveness(),
        *probe_clio_core_daemon_memory(env=env),
        *probe_cte_cold_tier_disk(env=env),
        *probe_cte_metadata_log(env=env),
        *probe_clio_core_write_health(env=env),
        *probe_clio_core_search_health(env=env),
    ]
