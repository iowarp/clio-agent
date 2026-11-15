"""The ``clio_core_metadata_restore`` health row (owner module, FN-14).

Says whether this boot's clio-core store came back whole: a torn metadata record cut out
before the daemon started (READY, with what was kept aside), a metadata log that could not
be read (UNAVAILABLE: the attach was refused), or the daemon's own report that its
metadata restore stopped early (UNAVAILABLE: conversations written before may be missing,
and the scratch sweep keeps their folders). No row when nothing happened.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime.status import IntegrationState, IntegrationStatus

_NAME = "clio_core_metadata_restore"
_SOURCE = "runtime:clio_core_metadata_log"


def probe_clio_core_metadata_restore(
    *,
    outcomes: list[dict[str, Any]] | None = None,
    failures: list[str] | None = None,
    damaged: dict[str, str] | None = None,
) -> list[IntegrationStatus]:
    """One row for this process's metadata-log findings, else none.

    Args:
        outcomes: Injected repair outcomes for testing; defaults to this process's.
        failures: Injected daemon restore-failure lines; defaults to the spawned daemon's.
        damaged: Injected unreadable session records; defaults to this process's.
    """
    from clio_agent.arc import clio_core_wal_repair as wal  # noqa: PLC0415
    from clio_agent.gact.session_record_damage import unreadable_sessions  # noqa: PLC0415

    damaged = unreadable_sessions() if damaged is None else damaged

    found = wal.outcomes() if outcomes is None else outcomes
    failed = wal.restore_failures() if failures is None else failures
    unreadable = [item for item in found if item.get("reason") == wal.UNREADABLE]
    repaired = [item for item in found if item.get("reason") == wal.REPAIRED]
    if failed or unreadable:
        reason = wal.RESTORE_FAILED if failed else wal.UNREADABLE
        return [
            IntegrationStatus(
                name=_NAME,
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    "clio-core could not read all of its metadata log, so conversations "
                    "and records saved before this start may be missing. Nothing was "
                    "deleted: the files are unchanged and session folders are kept."
                ),
                config_source=_SOURCE,
                next_action=(
                    "Stop CLIO and report the metadata.log.* files named in the details; "
                    "do not delete the store."
                ),
                fallback="none",
                details={
                    "reason": reason,
                    "daemon_log": failed[:5],
                    "logs": unreadable,
                    "sessions_unreadable": damaged,
                },
                required=True,
            )
        ]
    if damaged:
        return [
            IntegrationStatus(
                name=_NAME,
                state=IntegrationState.DEGRADED,
                summary=(
                    f"{len(damaged)} conversation record(s) read back damaged from clio-core "
                    "and cannot open; they are kept, not deleted (their folders and "
                    "transcripts stay)."
                ),
                config_source=_SOURCE,
                next_action=(
                    "Do not delete the store. Report the session ids in the details with "
                    "the clio-core metadata.log.* and storage.bin_node0.alloc_log files."
                ),
                fallback="none",
                details={"reason": "session_record_unreadable", "sessions_unreadable": damaged},
                required=False,
            )
        ]
    if repaired:
        return [
            IntegrationStatus(
                name=_NAME,
                state=IntegrationState.READY,
                summary=(
                    f"clio-core's metadata log had {len(repaired)} torn record(s) from a "
                    "daemon stopped while it wrote; they were cut out before the start, so "
                    "the records after them load again. The cut bytes are kept beside the log."
                ),
                config_source=_SOURCE,
                next_action="No action required.",
                details={"reason": wal.REPAIRED, "repairs": repaired},
                required=False,
            )
        ]
    return []


__all__ = ["probe_clio_core_metadata_restore"]
