"""The ``sandbox`` doctor row (DEGRADED never ERROR — the floor is legal) (#975/#976).

Split out of the :mod:`clio_agent.runtime.sandbox` ladder owner to keep it under the
file-size ratchet. Reports the resolved confinement backend as an
:class:`~clio_agent.runtime.status.IntegrationStatus`: READY when an OS fence is active,
DEGRADED (surfaced, never an error) on the honest floor, SKIPPED when no server boot resolved
a backend. Re-exported from :mod:`clio_agent.runtime.sandbox` so callers keep reaching
``sandbox.probe_sandbox``.
"""

from __future__ import annotations

import logging
import sys
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

    READY when an OS fence is active (srt / Landlock); DEGRADED (surfaced, never an error
    state) on the honest floor, because a missing fence is a *legal* configuration (the
    advisory file_policy still applies; HPC/no-npm hosts are floor-only). Cites the
    mechanism, the typed reason and the srt/landlock detection details.
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
            required=False,
        )

    details: dict[str, Any] = {
        "reason": resolved.reason,
        "mechanism": resolved.mechanism,
        "active": resolved.active,
        **resolved.details,
    }
    if resolved.active and resolved.mechanism in sb.KNOWN_MECHANISMS - {sb.MECHANISM_NONE}:
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
            required=False,
        )

    srt = resolved.details.get("srt", {}) if isinstance(resolved.details, dict) else {}
    if resolved.reason == sb.REASON_WINDOWS_UNPROVISIONED:
        next_action = "Run `clio sandbox setup` (B3) to provision the Windows write fence."
    elif sys.platform.startswith("win") and resolved.reason in {
        sb.REASON_SRT_NOT_INSTALLED,
        sb.REASON_SRT_NODE_MISSING,
    }:
        # Windows srt precondition gap: the fence needs srt BEFORE `clio sandbox setup` (#977).
        next_action = (
            f"Install srt (`npm install -g {sb.SRT_PACKAGE_NAME}`), then run `clio sandbox setup`."
        )
    elif resolved.reason == sb.REASON_DISABLED:
        next_action = "Set sandbox.enabled=true (CLIO_SANDBOX_ENABLED) to resolve a fence."
    elif resolved.reason == sb.REASON_SRT_VERSION_UNSUPPORTED:
        next_action = (
            f"Installed {sb.SRT_PACKAGE_NAME} v{srt.get('version') or '?'} is below the "
            f"validated floor v{'.'.join(map(str, _min_srt()))}; upgrade it for the OS fence."
        )
    else:
        next_action = (
            f"Install {sb.SRT_PACKAGE_NAME} (needs node>={sb.SRT_MIN_NODE_VERSION[0]}."
            f"{sb.SRT_MIN_NODE_VERSION[1]}"
            f"{', socat on Linux' if sys.platform.startswith('linux') else ''}), or run on a "
            "Landlock-capable kernel (>=5.13), for the OS write fence; the advisory "
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
        required=False,
    )


def _min_srt() -> tuple[int, int, int]:
    from clio_agent.runtime.sandbox_srt import SRT_MIN_SUPPORTED_VERSION  # noqa: PLC0415

    return SRT_MIN_SUPPORTED_VERSION


__all__ = ["emit_boot_state_event", "probe_sandbox"]
