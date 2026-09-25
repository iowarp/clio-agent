"""ALCF / Argonne gateway dialect adapter (model-capabilities brief Part 6, ALCF section).

ALCF fronts a fleet of vLLM-backed jobs behind ``/jobs`` (kept as-is by
:class:`clio_agent.providers.handshake.argonne.ArgonneHandshake`, which already
extracts every DEPLOYMENT fact the gateway's own ``/models`` row self-reports:
``max_model_len``, ``reasoning_parser``, ``tool_call_parser``,
``enable_auto_tool_choice``). The brief asks to additionally read each RUNNING
job's own vLLM endpoint the way :mod:`.vllm` does (``/version`` for a
fingerprint, ``/v1/models`` for the row vLLM itself would report).

That per-job endpoint URL is not part of any recorded ALCF response this repo
ships fixtures for, and ALCF is reached over an authenticated Globus gateway
this environment has no test credentials for (no real network probes against
a user service) -- so this is deliberately a thin, best-effort wrapper around
the SAME :mod:`.vllm` parsing this module reuses rather than a new ALCF-specific
parser: a job dict that names its own endpoint (``endpoint``/``url``/``api_base``)
gets probed with :mod:`.vllm`'s functions; one that doesn't is left exactly as
the existing gateway-row facts already describe it. A failure never sinks
discovery, matching :meth:`ArgonneHandshake._discover_hot_models`'s own
best-effort contract.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from clio_agent.providers.capabilities.dialects import vllm
from clio_agent.providers.capabilities.records import DeploymentCapabilities, EndpointCapabilities

logger = logging.getLogger(__name__)

#: Field names an ALCF ``/jobs`` row might name its own per-job vLLM endpoint
#: under. Tried in order; the first present non-empty string wins. Kept as a
#: named constant (not inlined) so a confirmed real field name is a one-line
#: fix, not a re-read of this whole module.
_JOB_ENDPOINT_FIELDS: tuple[str, ...] = ("endpoint", "url", "api_base", "Endpoint")


def job_endpoint_url(job: Mapping[str, Any]) -> str | None:
    """The job's own vLLM endpoint URL, when the ``/jobs`` row names one."""
    for field_name in _JOB_ENDPOINT_FIELDS:
        value = job.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def probe_job_endpoint(
    client: Any, job: Mapping[str, Any], *, provider_id: str, model_id: str
) -> tuple[EndpointCapabilities, DeploymentCapabilities] | None:
    """Best-effort: probe one running job's OWN vLLM endpoint for richer facts.

    Returns ``None`` (never raises) when the job names no endpoint of its own,
    or when reaching it fails -- the caller keeps whatever the gateway's own
    ``/models``/``/jobs`` rows already established via
    :class:`clio_agent.providers.handshake.argonne.ArgonneHandshake`.
    """
    base = job_endpoint_url(job)
    if base is None:
        return None
    try:
        models_response = await client.get(f"{base.rstrip('/')}/v1/models")
        models_response.raise_for_status()
        models_payload = models_response.json()
        row = vllm.find_model_row(models_payload, model_id)
        if row is None:
            return None
        deployment = vllm.parse_models_row(row, provider_id=provider_id, api_base=base)
        version = None
        try:
            version_response = await client.get(f"{base.rstrip('/')}/version")
            if version_response.status_code < 400:
                version = version_response.json().get("version")
        except Exception:  # noqa: BLE001 - version is a nice-to-have, not required
            version = None
        endpoint = vllm.build_endpoint_capabilities(provider_id, base, model_id, version=version)
        return endpoint, deployment
    except Exception as exc:  # noqa: BLE001 - best-effort, mirrors _discover_hot_models
        logger.debug("alcf: per-job vLLM probe failed for %s: %s", base, exc)
        return None


__all__ = ["job_endpoint_url", "probe_job_endpoint"]
