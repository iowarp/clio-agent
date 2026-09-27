"""Bring back recorded provider support after the backend environment was replaced.

:mod:`clio_agent.providers.support_record` keeps the provider support a person
installed (and the floor of every in-place SDK update) in their user
configuration. This module compares that record with the RUNNING environment
at startup and reinstalls what is missing, with typed, observable state:

* a recorded provider kind whose support module is not importable is
  reinstalled through the same installer the Install button uses
  (:func:`clio_agent.providers.dependencies.ensure_provider_support`), so the
  release it installs is resolved exactly as a first install would be;
* a recorded component floor above the installed version (a desktop update
  shipped an older provider SDK than the person had updated to) re-runs the
  in-place component update, with its own staged job and rollback.

The comparison is against reality (``importlib`` finds the module or not, the
installed distribution version), never against a guess of what changed, so it
covers every way the environment can be replaced: a CLIO Desktop update that
swaps the bundled runtime, the in-place "Update all" agent update, a
reinstall, or a rebuilt tool environment.

States (:class:`RestoreJob.state`): ``restoring`` -> ``restored`` | ``failed``.
A failed restore keeps its typed ``error_code`` and the installer's diagnostic;
the provider row then says plainly that the restore failed, and the Install
button (or ``POST /v1/providers/support/restore``) retries it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

RestoreAction = Literal["install", "update"]
RestoreState = Literal["restoring", "restored", "failed"]

#: Typed reasons.
SUPPORT_MISSING = "support_missing"
COMPONENT_BELOW_RECORD = "component_below_record"
PROVIDER_SUPPORT_RESTORE_FAILED = "provider_support_restore_failed"

#: The status a provider row reports while its support is being restored.
SUPPORT_RESTORING_STATUS = "support_restoring"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RestoreStep:
    """One thing the running environment lacks compared with the record."""

    provider_kind: str
    action: RestoreAction
    reason: str
    floors: dict[str, str] = field(default_factory=dict)


@dataclass
class RestoreJob:
    """The observable state of one provider's restore."""

    provider_kind: str
    action: RestoreAction
    reason: str
    display_name: str
    state: RestoreState = "restoring"
    error_code: str = ""
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def to_wire(self) -> dict[str, Any]:
        """JSON shape served by ``GET /v1/providers/support/restores``."""
        return {
            "provider_kind": self.provider_kind,
            "display_name": self.display_name,
            "action": self.action,
            "reason": self.reason,
            "state": self.state,
            "error": {"code": self.error_code, "message": self.error} if self.error_code else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _older(installed: str, floor: str) -> bool:
    try:
        return Version(installed) < Version(floor)
    except InvalidVersion:
        return False


def display_name_for(provider_kind: str) -> str:
    """How a provider kind's support is named to a person."""
    from clio_agent.providers.dependencies import PROVIDER_SUPPORT  # noqa: PLC0415

    spec = PROVIDER_SUPPORT.get(provider_kind)
    if spec is not None:
        return spec.display_name
    return {"codex": "Codex"}.get(provider_kind, provider_kind)


def plan_restores(
    entries: Mapping[str, Mapping[str, str]],
    *,
    installed: Callable[[str], bool],
    version_of: Callable[[str], str],
) -> list[RestoreStep]:
    """What must be restored for the recorded ``entries`` in this environment.

    Args:
        entries: ``{provider kind: {distribution: floor}}`` from the record.
        installed: Whether a provider kind's support module is importable.
        version_of: A distribution's installed version (``""`` when absent).
    """
    from clio_agent.providers.components.registry import components_for  # noqa: PLC0415
    from clio_agent.providers.dependencies import PROVIDER_SUPPORT  # noqa: PLC0415

    steps: list[RestoreStep] = []
    for kind, floors in sorted(entries.items()):
        if kind in PROVIDER_SUPPORT and not installed(kind):
            steps.append(RestoreStep(kind, "install", SUPPORT_MISSING, dict(floors)))
            continue
        spec = components_for(kind)
        if spec is None:
            continue
        below = {
            name: floor
            for name, floor in floors.items()
            if name in spec.distributions and _older(version_of(name), floor)
        }
        if below:
            steps.append(RestoreStep(kind, "update", COMPONENT_BELOW_RECORD, below))
    return steps


class SupportRestorer:
    """Runs restores one at a time; the last job per provider stays readable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RestoreJob] = {}
        self._running = False
        self._record_error: dict[str, str] | None = None

    def note_record_read(self, reason: str, detail: str) -> None:
        """Remember whether the last plan could read the record (typed reason or clear)."""
        with self._lock:
            self._record_error = {"code": reason, "message": detail} if reason else None

    @property
    def record_error(self) -> dict[str, str] | None:
        """Why the last plan could not read the recorded support, if it could not."""
        with self._lock:
            return dict(self._record_error) if self._record_error else None

    def job(self, provider_kind: str) -> RestoreJob | None:
        """The latest restore job for ``provider_kind``."""
        with self._lock:
            return self._jobs.get(provider_kind)

    def jobs(self) -> list[RestoreJob]:
        """Every provider's latest restore job."""
        with self._lock:
            return list(self._jobs.values())

    @property
    def running(self) -> bool:
        """Whether a restore pass is in progress."""
        with self._lock:
            return self._running

    def claim(self, steps: list[RestoreStep]) -> list[RestoreJob]:
        """Mark ``steps`` as ``restoring`` (visible at once) and return their jobs.

        Raises:
            RuntimeError: A restore pass is already running.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("a provider support restore is already running")
            self._running = bool(steps)
            jobs = [
                RestoreJob(
                    step.provider_kind,
                    step.action,
                    step.reason,
                    display_name_for(step.provider_kind),
                )
                for step in steps
            ]
            for job in jobs:
                self._jobs[job.provider_kind] = job
            return jobs

    def execute(
        self,
        jobs: list[RestoreJob],
        *,
        install: Callable[[str], object],
        update: Callable[[str], Any],
        on_settled: Callable[[RestoreJob], None] | None = None,
    ) -> None:
        """Run claimed ``jobs`` in order on the calling thread.

        Args:
            jobs: Jobs returned by :meth:`claim`.
            install: Installs one provider kind's support (raises on failure).
            update: Runs one provider kind's component update to completion and
                returns its ``UpdateJob``.
            on_settled: Called after each job reaches a terminal state.
        """
        try:
            for job in jobs:
                self._execute_one(job, install=install, update=update)
                if on_settled is not None:
                    try:
                        on_settled(job)
                    except Exception:  # noqa: BLE001 - a notification hook must not hide the result
                        logger.exception(
                            "provider support restore hook failed reason=restore_hook_failed"
                        )
        finally:
            with self._lock:
                self._running = False

    def _execute_one(
        self,
        job: RestoreJob,
        *,
        install: Callable[[str], object],
        update: Callable[[str], Any],
    ) -> None:
        logger.info(
            "provider support restore started provider=%s action=%s reason=%s",
            job.provider_kind,
            job.action,
            job.reason,
        )
        try:
            if job.action == "install":
                install(job.provider_kind)
            else:
                outcome = update(job.provider_kind)
                if getattr(outcome, "stage", "") != "done":
                    raise _UpdateOutcomeError(
                        str(getattr(outcome, "error_code", "") or "component_update_failed"),
                        str(getattr(outcome, "error", "") or "the component update did not finish"),
                    )
        except _UpdateOutcomeError as exc:
            job.state, job.error_code, job.error = "failed", exc.code, str(exc)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a typed restore state
            job.state, job.error_code, job.error = (
                "failed",
                PROVIDER_SUPPORT_RESTORE_FAILED,
                str(exc),
            )
        else:
            job.state = "restored"
        job.finished_at = _now()
        log = logger.info if job.state == "restored" else logger.warning
        log(
            "%sprovider support restore finished provider=%s action=%s state=%s reason=%s detail=%s",
            "" if job.state == "restored" else "⚑ ",
            job.provider_kind,
            job.action,
            job.state,
            job.error_code,
            job.error[-600:],
        )

    def reset(self) -> None:
        """Forget every job (test isolation)."""
        with self._lock:
            self._jobs.clear()
            self._running = False
            self._record_error = None


class _UpdateOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: The process-wide restorer.
RESTORER = SupportRestorer()


def _not_installed_message(provider_kind: str) -> str:
    if provider_kind == "argonne":
        from clio_agent.providers.argonne_auth import ARGONNE_NOT_INSTALLED_MESSAGE  # noqa: PLC0415

        return ARGONNE_NOT_INSTALLED_MESSAGE
    from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
        CLAUDE_CODE_NOT_INSTALLED_MESSAGE,
    )

    return CLAUDE_CODE_NOT_INSTALLED_MESSAGE


def missing_support_status(provider_kind: str) -> tuple[str, str]:
    """``(status, message)`` for a provider whose support module is missing.

    ``support_restoring`` while a restore runs; ``install_required`` with the
    restore's failure spelled out after a failed restore (Install retries it);
    otherwise plain ``install_required``.
    """
    job = RESTORER.job(provider_kind)
    if job is not None and job.action == "install":
        if job.state == "restoring":
            return (
                SUPPORT_RESTORING_STATUS,
                f"Restoring {job.display_name} support after the update…",
            )
        if job.state == "failed":
            return (
                "install_required",
                f"CLIO could not restore {job.display_name} support after the update "
                f"({job.error_code}: {job.error[-300:]}). Install it again to retry.",
            )
    return "install_required", _not_installed_message(provider_kind)


def plan_boot_restore(
    *,
    installed: Callable[[str], bool] | None = None,
    version_of: Callable[[str], str] | None = None,
) -> list[RestoreStep]:
    """Plan what this environment lacks compared with the recorded support.

    Args:
        installed: Whether a provider kind's support is importable (default:
            this interpreter).
        version_of: A distribution's installed version (default: this
            interpreter's metadata).
    """
    from clio_agent.providers.components.status import installed_version  # noqa: PLC0415
    from clio_agent.providers.dependencies import support_installed  # noqa: PLC0415
    from clio_agent.providers.support_record import read_recorded_support  # noqa: PLC0415

    record = read_recorded_support()
    RESTORER.note_record_read(record.reason, record.detail)
    return plan_restores(
        record.entries,
        installed=installed or support_installed,
        version_of=version_of or installed_version,
    )


__all__ = [
    "COMPONENT_BELOW_RECORD",
    "PROVIDER_SUPPORT_RESTORE_FAILED",
    "RESTORER",
    "SUPPORT_MISSING",
    "SUPPORT_RESTORING_STATUS",
    "RestoreJob",
    "RestoreStep",
    "SupportRestorer",
    "display_name_for",
    "missing_support_status",
    "plan_boot_restore",
    "plan_restores",
]
