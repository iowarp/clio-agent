"""Desktop trigger for the Codex Windows protected-execution setup.

Owner module for :func:`run_sandbox_setup` — the seam the desktop's "Set up protected
execution" button (Infrastructure > Agent) calls to FIX the ``sandbox`` doctor row instead of
merely explaining it. It is a thin orchestration layer over the existing ``clio sandbox setup``
engine (:func:`clio_agent.runtime.sandbox_cli.provision_codex_windows`) — no provisioning logic
is duplicated here: this module forwards the same ``elevator``/``verifier``/``gate``
collaborators the CLI already supports, then force-re-resolves the confinement ladder
(:func:`clio_agent.runtime.sandbox.reresolve_after_setup`) and re-probes the doctor row
(:func:`clio_agent.runtime.sandbox_doctor.probe_sandbox`) so the caller gets the POST-setup
truth, never a stale cached row.

Concurrency: a module lock enforces ONE setup at a time. The self-elevating flow pops a native
UAC prompt and mutates the machine (creates the ``codexsandbox*`` accounts), so two overlapping
runs must never race each other or double-prompt. A second concurrent call gets the typed
:class:`SandboxSetupConflict` (``sandbox_setup_in_progress``) instead of silently queuing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from clio_agent.runtime import sandbox, sandbox_cli
from clio_agent.runtime.sandbox_doctor import probe_sandbox
from clio_agent.runtime.status import IntegrationStatus

logger = logging.getLogger(__name__)

#: Typed reason surfaced when a setup run is already in flight (module lock held).
REASON_SETUP_IN_PROGRESS = "sandbox_setup_in_progress"

#: Guards concurrent :func:`run_sandbox_setup` calls — the self-elevating flow must never race
#: itself (one UAC prompt / one account-provisioning pass at a time).
_SETUP_LOCK = threading.Lock()


class SandboxSetupConflict(RuntimeError):
    """A :func:`run_sandbox_setup` call arrived while a prior run still holds the lock."""

    def __init__(self) -> None:
        self.reason = REASON_SETUP_IN_PROGRESS
        super().__init__(self.reason)


@dataclass(frozen=True)
class SandboxSetupResult:
    """The outcome of one :func:`run_sandbox_setup` call: the verdict plus the fresh row.

    Attributes:
        status: One of ``sandbox_cli.OUTCOME_*`` or ``sandbox_cli.STATUS_NOT_WINDOWS`` — the SAME
            typed verdict ``clio sandbox setup`` reports from the CLI, carried through unchanged.
        reason: The matching typed reason from the provisioning result.
        elevated: Whether this call actually popped the UAC self-elevation prompt.
        row: The ``sandbox`` doctor row RE-PROBED after setup (never the pre-setup row) — READY
            once the fence is provisioned and enforcement-verified, DEGRADED otherwise with an
            honest typed reason.
    """

    status: str
    reason: str
    elevated: bool
    row: IntegrationStatus


def run_sandbox_setup(
    *,
    elevator: Any = None,
    verifier: Any = None,
    gate: Any = None,
) -> SandboxSetupResult:
    """Run the Codex Windows protected-execution setup, then re-report the doctor row.

    Delegates the actual provisioning to :func:`sandbox_cli.provision_codex_windows` — the SAME
    engine ``clio sandbox setup`` runs from the CLI — injectable with fakes so this seam is
    unit-testable without a real UAC elevation (``elevator``/``verifier``/``gate`` pass straight
    through; off-Windows ``provision_codex_windows`` is already a typed no-op via its own
    ``platform`` default, so this function needs no separate platform branch). After the run, the
    confinement ladder is force-re-resolved (:func:`sandbox.reresolve_after_setup`) so the row
    reflects the just-provisioned state rather than the cached boot resolve, then re-probed.

    Raises:
        SandboxSetupConflict: A setup run is already in flight (the module lock is held) — never
            queues behind it or double-elevates.
    """
    if not _SETUP_LOCK.acquire(blocking=False):
        raise SandboxSetupConflict()
    try:
        provision = sandbox_cli.provision_codex_windows(
            elevator=elevator, verifier=verifier, gate=gate
        )
        sandbox.reresolve_after_setup()
        row = probe_sandbox()
        logger.info(
            "sandbox setup run status=%s reason=%s elevated=%s row_state=%s",
            provision.status,
            provision.reason,
            provision.elevated,
            row.state.value,
        )
        return SandboxSetupResult(
            status=provision.status,
            reason=provision.reason,
            elevated=provision.elevated,
            row=row,
        )
    finally:
        _SETUP_LOCK.release()


__all__ = [
    "REASON_SETUP_IN_PROGRESS",
    "SandboxSetupConflict",
    "SandboxSetupResult",
    "run_sandbox_setup",
]
