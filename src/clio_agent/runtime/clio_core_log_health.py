"""The ``clio_core_daemon_log_flood`` health row (owner module).

Reports, on ``/v1/health`` and in the doctor, that the shared clio-core daemon is
flooding its log (:mod:`clio_agent.arc.runtime_log_flood`): DEGRADED while the pump's
flood record is fresh for the running daemon (it spins a core; the log stays capped),
READY with the typed record once this process replaced a flooding daemon.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime.status import IntegrationState, IntegrationStatus

_NAME = "clio_core_daemon_log_flood"
_SOURCE = "runtime:clio_core_log_pump"


def probe_clio_core_log_flood(
    *, active: dict[str, Any] | None = None, replaced: dict[str, Any] | None = None
) -> list[IntegrationStatus]:
    """One row for an active flood or a flood replacement in this process, else none.

    Args:
        active: Injected active flood record for testing; defaults to the live record.
        replaced: Injected replacement record for testing; defaults to this process's.
    """
    from clio_agent.arc import runtime_log_flood  # noqa: PLC0415

    flood = runtime_log_flood.active_flood() if active is None else active
    if flood:
        return [
            IntegrationStatus(
                name=_NAME,
                state=IntegrationState.DEGRADED,
                summary=(
                    f"clio-core daemon pid {flood.get('daemon_pid')} is repeating one log "
                    f"line at {flood.get('lines_per_s')} lines/s and keeps a CPU core busy; "
                    "its log is capped, so the disk is safe."
                ),
                config_source=_SOURCE,
                next_action=(
                    "Quit every CLIO client so the daemon is stopped and a fresh one starts, "
                    "or set CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE=1 to replace it now."
                ),
                fallback="none",
                details={"reason": runtime_log_flood.DAEMON_LOG_FLOOD, **flood},
                required=False,
            )
        ]
    found = runtime_log_flood.flood_replacement() if replaced is None else replaced
    if not found:
        return []
    return [
        IntegrationStatus(
            name=_NAME,
            state=IntegrationState.READY,
            summary=(
                f"clio-core daemon pid {found.get('daemon_pid')} was flooding its log "
                f"({found.get('lines_per_s')} lines/s); it was stopped "
                f"(path={found.get('stop_path')}) and a fresh daemon started on the same store."
            ),
            config_source=_SOURCE,
            next_action="No action required.",
            fallback="none",
            details=dict(found),
            required=False,
        )
    ]
