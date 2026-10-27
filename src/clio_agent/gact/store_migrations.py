"""Store migrations: run once at install or upgrade, only checked at boot (owner module).

Each migration converts what an older CLIO wrote into what this one reads, and records a
done marker in the clio-core store it converts (no separate store). ``clio-agent migrate``
(:mod:`clio_agent.gact.store_migrate_command`) runs every owed migration offline; the
installers and the desktop's install-time prepare step run that command, so a normal
start finds every marker and reads nothing else.

Boot only checks the markers (:func:`ensure_at_boot`, one record read each). A migration
that is still owed (an install that skipped the step, a runtime swapped in without it, a
store copied from an older CLIO) runs ONCE, at that boot, before ``ready``: it is timed
as the boot step ``migration.<name>``, logged with the typed reason
``migration_owed_at_boot``, and reported by the ``store_migrations`` health row. Boot does
not refuse: the desktop and SSH-deployed agents start unattended, and every migration
writes its marker last and is idempotent, so running it once is safe and the next boot
is back to marker reads.

Adding a migration is one function and one entry in :data:`MIGRATIONS`::

    def migrate_mail_lane(app) -> dict | None:
        ...  # idempotent; may be interrupted and rerun
        return {"sessions": n}  # details printed by the command and the health row

    StoreMigration("mail_lane", "Move mail to its own lane", migrate_mail_lane)

Without ``owed``, the runner keeps the marker for you: the migration is owed while the
task index has no ``meta.<name>`` record, and the runner writes it after ``run`` returns.
Entries run in tuple order; put a migration after the ones whose output it reads, and
call :func:`ensure_at_boot` from the boot site that first needs its output.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: Typed reason of a migration that boot found owed and ran once.
OWED_AT_BOOT = "migration_owed_at_boot"
#: Typed reason of a migration whose run returned without recording its marker.
MARKER_NOT_WRITTEN = "marker_not_written"
#: Typed reason of a migration whose run raised.
MIGRATION_FAILED = "migration_failed"

Status = Literal["already_done", "done", "failed"]


@dataclass(frozen=True)
class StoreMigration:
    """One store migration: a name (its marker), what it does, how to run it."""

    name: str
    summary: str
    run: Callable[[Any], dict[str, Any] | None]
    #: Cheap marker check (one record read). ``None``: the runner's ``meta.<name>`` marker.
    owed: Callable[[Any], bool] | None = None
    #: Typed reason logged and reported when boot finds it owed and runs it once.
    boot_reason: str = ""


@dataclass
class MigrationResult:
    """What one migration did, for the command's output and the health row."""

    name: str
    status: Status
    seconds: float = 0.0
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"name": self.name, "status": self.status, "seconds": self.seconds}
        if self.reason:
            row["reason"] = self.reason
        if self.details:
            row["details"] = self.details
        return row


# ---- the migrations ---------------------------------------------------------------


def _transcripts_owed(app: Any) -> bool:
    from clio_agent.gact.transcript_migration import IMPORT_DONE  # noqa: PLC0415

    if getattr(app.state, "message_store", None) is None and not getattr(
        app.state, "transcript_file", False
    ):
        return False  # transcript.file off: there is no legacy JSON to import
    return app.state.task_history.index.done_marker(IMPORT_DONE) is None


def _run_transcripts(app: Any) -> dict[str, Any]:
    from clio_agent.gact.transcript_migration import (  # noqa: PLC0415
        IMPORT_DONE,
        migrate_transcripts,
    )

    migrate_transcripts(app)
    marker = app.state.task_history.index.done_marker(IMPORT_DONE) or {}
    return {"sessions": marker.get("sessions", 0)}


def _task_event_lane_owed(app: Any) -> bool:
    from clio_agent.gact.task_event_lane import migration_owed  # noqa: PLC0415

    return migration_owed(app.state.task_history)


def _run_task_event_lane(app: Any) -> dict[str, Any]:
    from clio_agent.gact.task_event_lane import migrate_task_event_lane  # noqa: PLC0415

    report = migrate_task_event_lane(app.state.task_history)
    keys = ("sessions", "sessions_with_task_events", "events")
    return {key: report[key] for key in keys if key in report}


def _task_lane_reads_owed(app: Any) -> bool:
    from clio_agent.gact.task_lane_reads import reads_owed  # noqa: PLC0415

    return reads_owed(app.state.task_history)


def _run_task_lane_reads(app: Any) -> dict[str, Any]:
    from clio_agent.gact.task_lane_reads import migrate_task_lane_reads  # noqa: PLC0415

    return migrate_task_lane_reads(app.state.task_history)


def _run_session_records(app: Any) -> dict[str, Any]:
    # Imports only sessions the records do not hold, so a rerun after an interruption
    # neither duplicates nor overwrites; the runner's marker ends it for good.
    return app.state.sessions.import_legacy()


def _task_index_owed(app: Any) -> bool:
    return app.state.task_history.index.marker() is None


def _run_task_index(app: Any) -> dict[str, Any]:
    from clio_agent.gact.task_migration import migrate_history  # noqa: PLC0415
    from clio_agent.gact.task_publication import settle_published_outcomes  # noqa: PLC0415

    history = app.state.task_history
    if not history.index_build_pending:
        history.restore()  # without the marker this is the one full replay the build reads
    migrate_history(app)
    history.complete_index_build()
    # The build derives an open record per terminal outcome; settle the published ones
    # here, so the next start's startup row reads only the work that is really open.
    settled = settle_published_outcomes(history)
    return {"replay_seconds": round(history.replay_seconds, 3), **settled}


def _artifact_index_owed(app: Any) -> bool:
    from clio_agent.gact.artifacts.registry_boot import artifact_index_owed  # noqa: PLC0415

    return artifact_index_owed(app)


def _run_artifact_index(app: Any) -> dict[str, Any]:
    from clio_agent.gact.artifacts.registry_boot import migrate_artifact_index  # noqa: PLC0415

    return migrate_artifact_index(app)


#: Every store migration, in run order. Name = marker name; never rename a shipped one.
MIGRATIONS: tuple[StoreMigration, ...] = (
    StoreMigration(
        "session_records_import",
        "Import the legacy sessions.json into one clio-core record per session",
        _run_session_records,
        boot_reason="session_records_import_owed",
    ),
    StoreMigration(
        "task_event_lane",
        "Copy each conversation's task events into their own lane (task trees read only it)",
        _run_task_event_lane,
        _task_event_lane_owed,
        boot_reason="task_event_lane_migration_owed",
    ),
    StoreMigration(
        "task_lane_reads",
        "Annotate each task-event lane so a task tree loads from the lane alone",
        _run_task_lane_reads,
        _task_lane_reads_owed,
        boot_reason="task_lane_reads_migration_owed",
    ),
    StoreMigration(
        "transcript_legacy_import",
        "Keep each conversation's legacy JSON transcript as evidence in clio-core",
        _run_transcripts,
        _transcripts_owed,
    ),
    StoreMigration(
        "task_index_build",
        "Convert retained task formats and build the task lookup and open-work records",
        _run_task_index,
        _task_index_owed,
    ),
    StoreMigration(
        "artifact_index",
        "Fold the artifact history once into the clio-core artifact records",
        _run_artifact_index,
        _artifact_index_owed,
    ),
)


def migration(name: str) -> StoreMigration:
    """The registered migration ``name`` (``KeyError`` names the known ones)."""
    for item in MIGRATIONS:
        if item.name == name:
            return item
    raise KeyError(f"unknown store migration {name!r}; known: {[m.name for m in MIGRATIONS]}")


# ---- running ----------------------------------------------------------------------


def is_owed(app: Any, item: StoreMigration) -> bool:
    """Whether ``item`` has not recorded its marker in this store."""
    if item.owed is not None:
        return item.owed(app)
    return app.state.task_history.index.done_marker(item.name) is None


def run_migration(app: Any, item: StoreMigration) -> MigrationResult:
    """Run ``item`` if owed, write the generic marker, and verify the marker exists."""
    if not is_owed(app, item):
        return MigrationResult(item.name, "already_done")
    started = time.monotonic()
    details = item.run(app) or {}
    if item.owed is None:
        history = app.state.task_history
        value = {"completed_at": datetime.now(UTC).isoformat(), **details}
        history.confirm_write(partial(history.index.put_done_marker, item.name, value))
    seconds = round(time.monotonic() - started, 3)
    if is_owed(app, item):
        return MigrationResult(item.name, "failed", seconds, MARKER_NOT_WRITTEN, details)
    return MigrationResult(item.name, "done", seconds, details=details)


def ensure_at_boot(app: Any, name: str) -> bool:
    """Boot's check of one marker; an owed migration runs once here, typed.

    Returns whether the migration ran at this boot. A failure is recorded for the
    health row and re-raised to the boot site's own error handling.
    """
    from clio_agent.gact import boot_timing  # noqa: PLC0415

    item = migration(name)
    report = _boot_report(app)
    if not is_owed(app, item):
        report.setdefault(name, MigrationResult(name, "already_done"))
        return False
    reason = item.boot_reason or OWED_AT_BOOT
    logger.warning(
        "store migration owed at boot reason=%s name=%s; running it once now. Installs and "
        "upgrades run `clio-agent migrate` so boot does not have to.",
        reason,
        name,
    )
    started = time.monotonic()
    try:
        with boot_timing.step(app, f"migration.{name}"):
            result = run_migration(app, item)
    except Exception as exc:
        seconds = round(time.monotonic() - started, 3)
        report[name] = MigrationResult(
            name, "failed", seconds, MIGRATION_FAILED, {"error": repr(exc)}
        )
        raise
    if result.status == "done":
        result.reason = reason
    report[name] = result
    return True


def _boot_report(app: Any) -> dict[str, MigrationResult]:
    report = getattr(app.state, "store_migrations_boot", None)
    if report is None:
        report = {}
        app.state.store_migrations_boot = report
    return report


def _boot_cost(result: MigrationResult) -> str:
    # At boot the task-index replay already ran in the task_history_restore step.
    replay = result.details.get("replay_seconds")
    extra = f" + {replay:.1f}s replay in task_history_restore" if replay else ""
    return f"{result.name} ({result.seconds:.1f}s{extra})"


def health_rows(app: Any) -> list[Any]:
    """The ``store_migrations`` row for ``GET /v1/health`` (none before boot checked)."""
    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    report = dict(getattr(app.state, "store_migrations_boot", None) or {})
    if not report:
        return []
    rows = [result.as_dict() for result in report.values()]
    failed = [r for r in report.values() if r.status == "failed"]
    ran = [r for r in report.values() if r.status == "done"]
    if failed:
        state = IntegrationState.UNAVAILABLE
        summary = "Store migration failed at boot: " + ", ".join(
            f"{r.name} ({r.reason})" for r in failed
        )
        next_action = "Stop CLIO, run `clio-agent migrate` and read its output."
    elif ran:
        state = IntegrationState.DEGRADED
        summary = (
            "This install skipped `clio-agent migrate`; boot ran "
            + ", ".join(_boot_cost(r) for r in ran)
            + " once."
        )
        next_action = "Nothing to do: the markers are recorded and later boots only check them."
    else:
        state = IntegrationState.READY
        summary = f"Store migrations done; boot checked {len(report)} marker(s) and ran none."
        next_action = ""
    return [
        IntegrationStatus(
            name="store_migrations",
            state=state,
            summary=summary,
            config_source="clio-core markers (clio-agent migrate)",
            next_action=next_action,
            details={"migrations": rows},
            required=False,
        )
    ]


__all__ = [
    "MARKER_NOT_WRITTEN",
    "MIGRATIONS",
    "MIGRATION_FAILED",
    "OWED_AT_BOOT",
    "MigrationResult",
    "StoreMigration",
    "ensure_at_boot",
    "health_rows",
    "is_owed",
    "migration",
    "run_migration",
]
