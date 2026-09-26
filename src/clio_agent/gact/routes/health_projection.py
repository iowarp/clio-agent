"""Projection helpers for the GACT health route."""

from __future__ import annotations

from typing import Literal

from clio_agent.gact.providers.boot_selection import LM_PROVIDER_UNCONFIGURED
from clio_agent.gact.types import Integration
from clio_agent.runtime.status import (
    IntegrationState,
    IntegrationStatus,
    RuntimeReport,
)

_PROBE_STATE_TO_WIRE: dict[str, Literal["ready", "degraded", "unavailable"]] = {
    IntegrationState.READY.value: "ready",
    IntegrationState.SKIPPED.value: "ready",
    IntegrationState.DEGRADED.value: "degraded",
    IntegrationState.MISCONFIGURED.value: "degraded",
    IntegrationState.UNAVAILABLE.value: "unavailable",
}


def integration_to_wire(item: IntegrationStatus) -> Integration:
    """Project one runtime probe to the health wire without losing detail."""

    return Integration(
        name=item.name,
        status=_PROBE_STATE_TO_WIRE.get(item.state.value, "degraded"),
        detail=item.summary,
        summary=item.summary,
        config_source=item.config_source or None,
        next_action=item.next_action or None,
        endpoint=item.endpoint,
        required=item.required,
        reason=_row_reason(item),
    )


def _row_reason(item: IntegrationStatus) -> str | None:
    """The row's typed ``details["reason"]`` for the wire, when the probe set one."""

    reason = (item.details or {}).get("reason")
    return str(reason) if reason else None


def unconfigured_provider_report(
    report: RuntimeReport, *, provider_configured: bool
) -> RuntimeReport:
    """Report an unselected LM provider as ``unconfigured`` -- a setup choice, not an outage.

    Until the user picks a provider (``PUT /v1/providers/lm``, the config file, or
    ``CLIO_LM_PROVIDER`` -- never the committed ``lm_studio`` default), the probe
    engine's default-LM-Studio row says nothing true about this server: a headless
    remote host has no LM Studio by design. The row becomes SKIPPED (wire ``ready``,
    not required) with the typed reason ``lm_provider_unconfigured``.
    """

    if provider_configured:
        return report
    integrations = [
        IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.SKIPPED,
            summary="Choose a language model when starting a session.",
            config_source="session:model-selection",
            next_action="Select a provider and model in the message composer.",
            details={"reason": LM_PROVIDER_UNCONFIGURED},
            required=False,
        )
        if item.name == "lm_provider"
        else item
        for item in report.integrations
    ]
    return RuntimeReport(integrations=integrations)
