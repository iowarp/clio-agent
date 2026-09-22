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
import sys
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


def setup_in_progress() -> bool:
    """Whether a :func:`run_sandbox_setup` call currently holds the module lock.

    Non-blocking, non-mutating peek (``Lock.locked()``) so the desktop's ``GET
    /v1/system/sandbox`` row can show the button's busy/progress state without racing a
    concurrent setup's own ``acquire``.
    """
    return _SETUP_LOCK.locked()


def _fleet_already_running(app: Any) -> bool:
    """Cheap, truthful check: has any MCP namespace in THIS process already connected?

    A connected namespace means its backend already spawned a real stdio child (#932's
    ``_connected_namespaces``, stamped on a namespace's FIRST routed call) — under whatever
    confinement state was resolved AT THAT TIME. If that was before this setup run activated a
    fence, the already-running child is not covered by it.

    Walks the agent's default (no-workspace) executor plus every cached per-workspace executor
    (``ClioAgent._workspace_state()``). Reads the private ``_connected_namespaces`` /
    ``_namespace_clients`` attributes via ``getattr`` rather than a new public accessor: both
    ``SyncMCPToolExecutor`` (``tools/execution.py``) and ``AsyncMCPToolExecutor``
    (``tools/mcp_executor.py``) sit exactly at their #775 file-size ratchet baseline, so a new
    property cannot land in either without shrinking something else first. Best-effort: any
    failure to introspect the agent is logged and treated as "no fleet observed" — this signal
    only ever WIDENS an already-successful setup to a more cautious DEGRADED, never blocks the
    setup itself.

    Two signals count as "already running", either is sufficient:

    * ``_connected_namespaces`` — stamped on a namespace's FIRST ROUTED tool call (#932).
    * ``_namespace_clients`` — populated the moment a namespace's stdio child is actually
      SPAWNED, which happens earlier: session prewarm (``gact/mcp_readiness.py``'s
      ``prepare_namespace`` → ``_connect_namespace``) connects a namespace with no routed call
      at all, so ``_connected_namespaces`` alone missed an already-spawned prewarmed fleet
      (#A5 review).

    An executor reporting itself ``closed`` is skipped entirely — its leftover client/namespace
    bookkeeping describes a fleet that is no longer running, not one this fence must worry about.
    """
    agent = getattr(getattr(app, "state", None), "agent", None) if app is not None else None
    if agent is None:
        return False
    executors: list[Any] = []
    default_executor = getattr(agent, "tool_executor", None)
    if default_executor is not None:
        executors.append(default_executor)
    workspace_state = getattr(agent, "_workspace_state", None)
    if callable(workspace_state):
        try:
            _lock, cached, _leases = workspace_state()
            executors.extend(cached.values())
        except Exception as exc:  # noqa: BLE001 - best-effort signal must never break setup
            logger.warning(
                "sandbox fleet probe skipped reason=workspace_state_unreadable error=%r", exc
            )
    for executor in executors:
        if getattr(executor, "closed", False):
            continue
        async_executor = getattr(executor, "_async_executor", executor)
        if getattr(async_executor, "_connected_namespaces", None):
            return True
        if getattr(async_executor, "_namespace_clients", None):
            return True
    return False


def run_sandbox_setup(
    *,
    elevator: Any = None,
    verifier: Any = None,
    gate: Any = None,
    app: Any = None,
    platform: str = sys.platform,
) -> SandboxSetupResult:
    """Run the Codex Windows protected-execution setup, then re-report the doctor row.

    Delegates the actual provisioning to :func:`sandbox_cli.provision_codex_windows` — the SAME
    engine ``clio sandbox setup`` runs from the CLI — injectable with fakes so this seam is
    unit-testable without a real UAC elevation (``elevator``/``verifier``/``gate`` pass straight
    through). ``platform`` is forwarded to ``provision_codex_windows`` explicitly rather than
    relying on ITS OWN ``platform: str = sys.platform`` default: that default is bound once, at
    ``sandbox_cli`` module IMPORT time, so a test importing on a non-Windows CI runner can never
    override it after the fact by monkeypatching ``sys.platform`` — only an explicit keyword
    argument threaded through every call in the chain can. Production callers never pass it, so
    behaviour there is unchanged (the route only reaches this function after its OWN live
    ``sys.platform.startswith("win")`` check already passed). After the run, the confinement
    ladder is force-re-resolved (:func:`sandbox.reresolve_after_setup`) so the row reflects the
    just-provisioned state rather than the cached boot resolve, then re-probed.

    ``app`` (the FastAPI app, passed by the route) is used ONLY to check whether an MCP tool
    fleet already spawned in this process BEFORE the fence just activated (see
    :func:`_fleet_already_running`) — those already-running stdio children are not retroactively
    covered by a fence that provisions after they started. When that happens
    :func:`sandbox.mark_fence_pending_restart` is called so the re-probed row (and ``/v1/health``,
    which reads the same probe) reports an honest DEGRADED instead of a false READY.

    Raises:
        SandboxSetupConflict: A setup run is already in flight (the module lock is held) — never
            queues behind it or double-elevates.
    """
    if not _SETUP_LOCK.acquire(blocking=False):
        raise SandboxSetupConflict()
    try:
        before = sandbox.current_state()
        was_active_before = bool(before is not None and before.active)
        provision = sandbox_cli.provision_codex_windows(
            elevator=elevator, verifier=verifier, gate=gate, platform=platform
        )
        after = sandbox.reresolve_after_setup()
        if not was_active_before and after.active and _fleet_already_running(app):
            sandbox.mark_fence_pending_restart()
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
    "setup_in_progress",
]
