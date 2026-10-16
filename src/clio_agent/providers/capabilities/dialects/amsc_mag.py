"""Deployment token limits reported by MAG's authenticated ``/v1/models`` list."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from clio_agent.providers.capabilities.dialects import cloud
from clio_agent.providers.capabilities.records import DeploymentCapabilities, Fact, unknown


def parse_models_row(
    row: Mapping[str, Any], *, provider_id: str, api_base: str, observed_at: str
) -> DeploymentCapabilities:
    """Keep the exact wire id and provider-reported limits, without name inference.

    Missing or invalid limits remain unknown. These are deployment limits,
    not architecture ceilings, and input/output are independent facts.
    """
    deployment = cloud.build_deployment_capabilities(provider_id, api_base, str(row.get("id", "")))

    def limit(field: str) -> Fact[int]:
        value = row.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return Fact(value, "server_report", observed_at, f"MAG /v1/models {field}")
        return unknown()

    return replace(
        deployment,
        context_served=limit("max_input_tokens"),
        output_max=limit("max_output_tokens"),
    )
