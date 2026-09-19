"""Projection helpers for the GACT health route."""

from __future__ import annotations

import os
from typing import Literal

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
    )


def desktop_provider_report(
    report: RuntimeReport,
    *,
    lm_config: object | None,
    provider_configured: bool,
) -> RuntimeReport:
    """Make an unselected desktop provider an optional setup choice, not an outage."""

    if (
        os.environ.get("CLIO_DESKTOP_BOOT_HEARTBEAT") != "1"
        or lm_config is not None
        or provider_configured
    ):
        return report
    integrations = [
        IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.SKIPPED,
            summary="Choose a language model when starting a session.",
            config_source="session:model-selection",
            next_action="Select a provider and model in the message composer.",
            required=False,
        )
        if item.name == "lm_provider"
        else item
        for item in report.integrations
    ]
    return RuntimeReport(integrations=integrations)
