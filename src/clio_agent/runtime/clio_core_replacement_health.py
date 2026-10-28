"""The ``clio_core_daemon_replaced`` health row (owner module).

Reports, on ``/v1/health`` and in the doctor, that this process found the shared
clio-core daemon outliving clients that all crashed and replaced it instead of
adopting it (:mod:`clio_agent.arc.crashed_clients`). READY: the fresh daemon serves
the same store and nothing is owed; the row keeps the replacement visible and typed.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime.status import IntegrationState, IntegrationStatus


def probe_clio_core_daemon_replacement(
    *, record: dict[str, Any] | None = None
) -> list[IntegrationStatus]:
    """One row when this process replaced a daemon its crashed clients left, else none.

    Args:
        record: Optional injected replacement record for testing; defaults to the
            live process record.
    """
    from clio_agent.arc.crashed_clients import daemon_replacement  # noqa: PLC0415

    found = daemon_replacement() if record is None else record
    if not found:
        return []
    summary = (
        f"clio-core daemon pid {found.get('daemon_pid')} outlived clients that crashed "
        f"({found.get('crashed_clients')}); it was stopped (path={found.get('stop_path')}) "
        f"and a fresh daemon started on the same store in {found.get('seconds')}s."
    )
    return [
        IntegrationStatus(
            name="clio_core_daemon_replaced",
            state=IntegrationState.READY,
            summary=summary,
            config_source="runtime:clio_core_attach",
            next_action="No action required.",
            fallback="none",
            details=dict(found),
            required=False,
        )
    ]
