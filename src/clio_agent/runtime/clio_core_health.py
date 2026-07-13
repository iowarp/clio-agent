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

from collections.abc import Mapping

from clio_agent.arc.clio_core_config import RamTierCap, effective_ram_cap, parse_capacity_bytes
from clio_agent.runtime.humanize import format_bytes
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

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

    * MISCONFIGURED when the config file declares an unparseable cap (fail-loud) or no
      ram tier at all;
    * DEGRADED when the effective cap is ``0g`` (= 80% of system DRAM — the #890
      footgun), with the exact remediation;
    * READY otherwise, naming the bounded cap (raw string + human bytes).

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
        return [
            IntegrationStatus(
                name="clio_core_ram_cap",
                state=IntegrationState.MISCONFIGURED,
                summary=(
                    "clio-core CTE config declares no ram hot tier (no cte_ram_tier "
                    "capacity_limit); the memory↔disk hierarchy is not bounded."
                ),
                config_source=source,
                next_action=_REMEDIATION,
                endpoint=endpoint,
                details={**details, "reason": "ram_tier_missing"},
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
            endpoint=details["config_path"] or None,
            fallback="local",
            details=details,
            required=True,
        )
    ]


def probe_clio_core_health(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Aggregate the clio-core doctor rows: init degrade (#897) + ram cap (#890) + liveness (#892).

    A single collection seam so the doctor wires ONE call for all clio-core sub-checks.

    Args:
        env: Environment mapping forwarded to :func:`probe_clio_core_ram_cap`.

    Returns:
        The concatenated init-degradation, ram-cap, and liveness rows (each may be empty).
    """
    return [
        *probe_clio_core_init_degradation(),
        *probe_clio_core_ram_cap(env=env),
        *probe_clio_core_liveness(),
    ]
