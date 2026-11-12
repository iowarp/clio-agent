"""The ``sandbox`` doctor row (a required DEGRADED state when no fence is active).

Split out of the :mod:`clio_agent.runtime.sandbox` ladder owner to keep it under the
file-size ratchet. Reports the resolved confinement backend as an
:class:`~clio_agent.runtime.status.IntegrationStatus`: READY when an OS fence is active,
DEGRADED on the honest floor, SKIPPED when no server boot resolved a backend. The advisory
fallback keeps CLIO usable, but it does not make OS confinement optional. Re-exported from
:mod:`clio_agent.runtime.sandbox` so callers keep reaching
``sandbox.probe_sandbox``.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.runtime import sandbox as sb
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

logger = logging.getLogger(__name__)


def emit_boot_state_event(app: Any, state: "sb.SandboxResult | None") -> None:
    """Emit the boot ``sandbox.state`` conformance event (#975), best-effort.

    Mirrors the ``artifact.cas.tmp_swept`` boot-event pattern: a trace-only semantic event
    stamping the resolved OS write-confinement mechanism + typed reason so the conformance
    floor is queryable per boot. Uses the boot sid (``""``). Never blocks agent readiness — a
    failed emit is logged with a typed reason (no silent path). Called from the gact lifespan
    once ARC (the highway source) is live; the app only calls it (no accretion).
    """
    if state is None:
        return
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            "",
            "sandbox.state",
            status="completed",
            summary=(
                f"OS write-confinement resolved: mechanism={state.mechanism}, "
                f"active={state.active}, reason={state.reason}."
            ),
            actor={"mechanism": "harness"},
            payload={
                "mechanism": state.mechanism,
                "active": state.active,
                "reason": state.reason,
                "details": state.details,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a boot conformance emit must never block readiness
        logger.warning(
            "sandbox state event emit skipped reason=sandbox_state_emit_failed error=%r", exc
        )


def probe_sandbox(*, state: sb.SandboxResult | None = None) -> IntegrationStatus:
    """Report the confinement backend as a doctor row (#975/#976).

    READY when an OS fence is active (Codex / Landlock); DEGRADED on the honest floor. The
    advisory file policy keeps execution available, but the missing OS fence remains a required
    safety problem. Cites the mechanism, typed reason, and codex/landlock detection details.
    """
    resolved = state if state is not None else sb.current_state()
    if resolved is None:
        return IntegrationStatus(
            name="sandbox",
            state=IntegrationState.SKIPPED,
            summary="Confinement backend not resolved in this process (no server boot).",
            config_source="runtime:sandbox",
            next_action="Start the gact server to resolve the confinement backend.",
            details={"reason": sb.REASON_NOT_INSTALLED},
            required=True,
        )

    details: dict[str, Any] = {
        "reason": resolved.reason,
        "mechanism": resolved.mechanism,
        "active": resolved.active,
        **resolved.details,
    }
    if resolved.active and resolved.mechanism in sb.KNOWN_MECHANISMS - {sb.MECHANISM_NONE}:
        if sb.fence_pending_restart():
            # #A6 review: a fence that just activated does NOT retroactively cover an MCP tool
            # fleet this process already spawned before setup ran -- those stdio children keep
            # running outside it until CLIO restarts. Reporting READY here would be a silent
            # false-green; both this row and /v1/health read through the same probe, so they
            # stay consistent automatically.
            return IntegrationStatus(
                name="sandbox",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"OS write-confinement (mechanism={resolved.mechanism}) was just activated, "
                    "but an MCP tool fleet already spawned before that activation is not "
                    "covered by it."
                ),
                config_source="runtime:sandbox",
                next_action="Restart CLIO to fence already-running tool servers.",
                fallback="fence-active-pending-restart",
                details={**details, "reason": sb.REASON_FENCE_PENDING_RESTART},
                required=True,
            )
        net = (
            resolved.details.get("net_enforcement", "")
            if isinstance(resolved.details, dict)
            else ""
        )
        return IntegrationStatus(
            name="sandbox",
            state=IntegrationState.READY,
            summary=(
                f"OS write-confinement active (mechanism={resolved.mechanism}"
                f"{f', network={net}' if net else ''})."
            ),
            config_source="runtime:sandbox",
            next_action="No action required.",
            capabilities=["write-fence"],
            details=details,
            required=True,
        )

    from clio_agent.runtime import sandbox_codex as sc  # noqa: PLC0415
    from clio_agent.runtime.execution_environment import bundled_root  # noqa: PLC0415
    from clio_agent.runtime.sandbox_codex_mxc import (  # noqa: PLC0415
        PACKAGED_PYTHON_ACTION,
        REASON_MXC_PACKAGED_PYTHON,
    )
    from clio_agent.runtime.sandbox_codex_probe import (  # noqa: PLC0415
        REASON_CODEX_LINUX_FENCE_UNAVAILABLE,
    )

    codex_missing = (sc.REASON_CODEX_NOT_INSTALLED, sc.REASON_CODEX_VERSION_UNSUPPORTED)
    if resolved.reason in codex_missing and bundled_root() is not None:
        # The Desktop ships codex inside its runtime: a missing one is a damaged app,
        # and a global npm install would not be the binary this runtime resolves.
        next_action = (
            "CLIO Desktop's bundled sandbox (codex) is missing or unusable; reinstall "
            "CLIO Desktop to restore it."
        )
    elif resolved.reason in codex_missing:
        # Codex backend floor: guide the operator to install/upgrade codex.
        next_action = "Install codex: npm install -g @openai/codex"
    elif resolved.reason == sc.REASON_CODEX_VERSION_PROBE_UNRESPONSIVE:
        # Installed but `codex --version` did not answer (slow host / hung CLI) (#1577).
        next_action = "Codex is installed but did not answer `codex --version`; re-run the check."
    elif resolved.reason == sc.REASON_CODEX_WINDOWS_UNPROVISIONED:
        # Codex win32 backend: the dedicated sandbox accounts are not provisioned yet (#1026).
        next_action = "Run `clio sandbox setup` to provision the Codex Windows fence."
    elif resolved.reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED:
        # Codex win32 backend: accounts exist but the write fence could not be verified (#1026).
        next_action = (
            "The Windows sandbox accounts exist, but this CLIO installation has no successful "
            "verification receipt. Open Infrastructure > CLIO hosts > Protected execution and select "
            "Set up protected execution, or run `clio sandbox setup`, to verify it."
        )
    elif resolved.reason == REASON_CODEX_LINUX_FENCE_UNAVAILABLE:
        detail = (resolved.details.get("codex_probe") or {}).get("detail") or "no detail"
        next_action = (
            f"Codex is installed, but its bubblewrap fence cannot start a child here ({detail}). "
            "Allow unprivileged user namespaces (`sysctl kernel.unprivileged_userns_clone=1`; "
            "on Ubuntu 23.10+ `sysctl kernel.apparmor_restrict_unprivileged_userns=0`; in a "
            "container, a seccomp profile that permits them), or use a Landlock-capable "
            "kernel (>=5.13), then restart CLIO."
        )
    elif resolved.reason == REASON_MXC_PACKAGED_PYTHON:
        next_action = PACKAGED_PYTHON_ACTION
    elif resolved.reason == sb.REASON_DISABLED:
        next_action = "Set sandbox.enabled=true (CLIO_SANDBOX_ENABLED) to resolve a fence."
    else:
        next_action = (
            "Install codex (`npm install -g @openai/codex`) for the cross-platform OS write "
            "fence, or run on a Landlock-capable kernel (>=5.13) on Linux; the advisory "
            "file_policy still applies meanwhile."
        )
    return IntegrationStatus(
        name="sandbox",
        state=IntegrationState.DEGRADED,
        summary=(
            f"No OS write-confinement (mechanism=none, reason={resolved.reason}); the "
            "advisory file_policy applies at the tool boundary. Out-of-root writes are "
            "recorded on the provenance floor, not yet prevented."
        ),
        config_source="runtime:sandbox",
        next_action=next_action,
        fallback="advisory-file-policy-only",
        details=details,
        required=True,
    )


__all__ = ["emit_boot_state_event", "probe_sandbox"]


#: Banner text for a resolved backend that leaves agent tools outside any OS fence. The
#: fail-open policy keeps CLIO usable; clients must show this and have the user acknowledge it.
SANDBOX_OFF_WARNING = (
    "Protected execution is off. Agent tools and the commands they run are not confined by an "
    "OS sandbox, so they can change any file your user account can. CLIO still checks "
    "file-tool paths and records writes outside your workspace, but it does not prevent them."
)
SANDBOX_PENDING_RESTART_WARNING = (
    "Protected execution was just set up, but tool servers started before that are still "
    "unconfined and can change any file your user account can until CLIO restarts."
)


def sandbox_acknowledgement(row: IntegrationStatus) -> dict[str, Any]:
    """The client-facing verdict for the start-up "protected execution is off" banner.

    ``active`` is True only when agent tools run inside an OS write fence right now (a READY
    row). ``resolved`` is False until a server boot has picked a backend, so a client polling
    during start-up waits instead of warning early. ``acknowledgement_required`` is
    ``resolved and not active``; ``warning`` is the banner text exactly when it is True.
    """
    details = row.details if isinstance(row.details, dict) else {}
    resolved = row.state is not IntegrationState.SKIPPED
    active = row.state is IntegrationState.READY
    required = resolved and not active
    warning = None
    if required:
        pending = details.get("reason") == sb.REASON_FENCE_PENDING_RESTART
        warning = SANDBOX_PENDING_RESTART_WARNING if pending else SANDBOX_OFF_WARNING
    mechanism = details.get("mechanism")
    return {
        "active": active,
        "resolved": resolved,
        "mechanism": str(mechanism) if mechanism else None,
        "acknowledgement_required": required,
        "warning": warning,
    }
