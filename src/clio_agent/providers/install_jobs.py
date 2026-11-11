"""Provider support installs as background jobs that outlive the request that started them.

A person clicks Install on one provider, then looks at another one, closes the
picker or reloads the window. The install must keep going, and every view must
be able to read where it is. So an install is a job owned by the backend, not by
the HTTP request:

* :meth:`InstallJobs.start` returns the provider's RUNNING job when there is one
  (a second click, or a second window, joins it) and otherwise starts a new job
  on a worker thread.
* Jobs run one at a time (``queued`` until the previous one finishes, then
  ``installing``), because every install changes the same runtime environment.
  Installs of several providers therefore queue rather than race.
* The last job of every provider stays readable (``done`` | ``failed``) until
  the next install of that provider replaces it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

InstallState = Literal["queued", "installing", "done", "failed"]

#: ``exception -> (typed error code, person-facing message)`` for a failed install.
FailureCopy = Callable[[BaseException], tuple[str, str]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InstallJob:
    """The observable state of one provider support install."""

    provider_id: str
    provider_kind: str
    state: InstallState = "queued"
    installed: bool = False
    instructions: str = ""
    error_code: str = ""
    error: str = ""
    diagnostic: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    _finished: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def running(self) -> bool:
        """Whether the job has not reached a terminal state."""
        return self.state not in {"done", "failed"}

    def to_wire(self) -> dict[str, Any]:
        """JSON shape of the job."""
        return {
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "state": self.state,
            "running": self.running,
            "installed": self.installed,
            "instructions": self.instructions,
            "error": (
                {"code": self.error_code, "message": self.error, "diagnostic": self.diagnostic}
                if self.error_code
                else None
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job finished (``True``) or ``timeout`` passed."""
        return self._finished.wait(timeout)

    async def wait_async(self) -> None:
        """Wait for the job without blocking the event loop."""
        await asyncio.to_thread(self._finished.wait)


class InstallJobs:
    """Every provider's latest support install; one install runs at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slot = threading.Lock()
        self._jobs: dict[str, InstallJob] = {}

    def job(self, provider_id: str) -> InstallJob | None:
        """The provider's latest job (running or finished), if it ever installed."""
        with self._lock:
            return self._jobs.get(provider_id)

    def jobs(self) -> list[InstallJob]:
        """Every provider's latest job, oldest first."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.started_at)

    def start(
        self,
        provider_id: str,
        provider_kind: str,
        install: Callable[[], bool],
        *,
        success_instructions: str,
        failure_copy: FailureCopy,
        on_finish: Callable[[InstallJob], None] | None = None,
    ) -> tuple[InstallJob, bool]:
        """Return ``(job, started)``: the running job joined, or a new one started."""
        with self._lock:
            current = self._jobs.get(provider_id)
            if current is not None and current.running:
                return current, False
            job = InstallJob(provider_id=provider_id, provider_kind=provider_kind)
            self._jobs[provider_id] = job
        threading.Thread(
            target=self._work,
            args=(job, install, success_instructions, failure_copy, on_finish),
            name=f"provider-install-{provider_id}",
            daemon=True,
        ).start()
        return job, True

    def _work(
        self,
        job: InstallJob,
        install: Callable[[], bool],
        success_instructions: str,
        failure_copy: FailureCopy,
        on_finish: Callable[[InstallJob], None] | None,
    ) -> None:
        with self._slot:
            job.state = "installing"
            try:
                job.installed = install()
                job.instructions = success_instructions
                job.state = "done"
            except Exception as exc:  # noqa: BLE001 - forwards: a typed failed job state
                code, message = failure_copy(exc)
                job.error_code, job.error, job.diagnostic = code, message, str(exc)
                job.state = "failed"
                logger.warning(
                    "provider support install failed provider=%s reason=%s detail=%s",
                    job.provider_id,
                    code,
                    exc,
                )
            finally:
                job.finished_at = _now()
        logger.info(
            "provider support install finished provider=%s state=%s installed=%s",
            job.provider_id,
            job.state,
            job.installed,
        )
        try:
            if on_finish is not None:
                on_finish(job)
        except Exception:  # noqa: BLE001 - a post-install hook must not hide the result
            logger.exception(
                "provider install on_finish hook failed reason=provider_install_hook_failed"
            )
        finally:
            # Waiters (the blocking POST) resume only after the hook retired the
            # provider's catalog entry, so their re-check reads the new truth.
            job._finished.set()


#: The backend's one install registry.
INSTALLS = InstallJobs()

__all__ = ["INSTALLS", "InstallJob", "InstallJobs", "InstallState"]
