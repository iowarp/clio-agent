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
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.dialects import vllm
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    task_fact,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Field names an ALCF ``/jobs`` row might name its own per-job vLLM endpoint
#: under. Tried in order; the first present non-empty string wins. Kept as a
#: named constant (not inlined) so a confirmed real field name is a one-line
#: fix, not a re-read of this whole module.
_JOB_ENDPOINT_FIELDS: tuple[str, ...] = ("endpoint", "url", "api_base", "Endpoint")


#: ALCF gateway ``framework`` -> the task that serving framework proves.
#: A row's ``framework`` names the service running the model; only a service
#: that serves exactly one kind of model decides the task. ``vllm`` (and the
#: Metis ``api`` framework) serve chat AND embedding models alike, so they are
#: deliberately absent: the task stays unknown for other sources to establish.
FRAMEWORK_TASKS: dict[str, str] = {
    "sam3service": "mask-generation",
}


def gateway_task_fact(row: Mapping[str, Any], *, observed_at: str) -> Fact[str]:
    """The task fact an ALCF ``/models`` row's ``framework`` proves (or unknown)."""
    framework = str(row.get("framework") or "").strip().lower()
    return task_fact(
        FRAMEWORK_TASKS.get(framework),
        source="server_report",
        observed_at=observed_at,
        detail=f"ALCF gateway /models framework={framework!r}",
    )


def gateway_row_identity(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(reasoning_parser, tool_call_parser)`` from a raw gateway ``/models`` row.

    Shared by :func:`parse_gateway_model_row` and the handshake's raw
    passthrough metadata, so the field names are written once. A falsy value
    normalizes to ``None`` (an empty string is not evidence of a parser name).
    """
    reasoning_parser = row.get("reasoning_parser") or None
    tool_call_parser = row.get("tool_call_parser") or None
    return reasoning_parser, tool_call_parser


def parse_gateway_model_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> DeploymentCapabilities:
    """Build one :class:`DeploymentCapabilities` from an ALCF gateway ``/models`` row.

    ALCF fronts vLLM, and this row's ``max_model_len``/``root`` are exactly
    what a vLLM ``/v1/models`` row would report, so those two fields (and the
    model link/fingerprint they feed) reuse :func:`clio_agent.providers.
    capabilities.dialects.vllm.parse_models_row` rather than a second copy of
    that mapping. ``reasoning_parser``/``tool_call_parser``/
    ``enable_auto_tool_choice`` are ALCF-gateway-specific extras vanilla vLLM's
    own ``/v1/models`` does not expose (brief Part 6 vLLM section: "Tools,
    vision and reasoning parsing depend on launch flags that vLLM does not
    expose"), so they are layered on here, not in ``.vllm``. Every row is an
    EXHAUSTIVE self-report -- an absent parser/flag is real negative evidence
    (``False``), never left unknown.
    """
    observed_at = observed_at or _now_iso()
    base = vllm.parse_models_row(row, provider_id=provider_id, api_base=api_base, observed_at=observed_at)

    reasoning_parser, tool_call_parser = gateway_row_identity(row)
    auto_tool = bool(row.get("enable_auto_tool_choice"))
    native_tool_calling = tool_call_parser is not None or auto_tool

    return replace(
        base,
        reasoning_enabled=Fact(
            value=reasoning_parser is not None,
            source="server_report",
            observed_at=observed_at,
            detail=f"ALCF gateway /models reasoning_parser={reasoning_parser!r}",
        ),
        tools_enabled=Fact(
            value=native_tool_calling,
            source="server_report",
            observed_at=observed_at,
            detail=(
                f"ALCF gateway /models tool_call_parser={tool_call_parser!r} "
                f"enable_auto_tool_choice={auto_tool!r}"
            ),
        ),
    )


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
        models_payload = await vllm.fetch_models(client, base)
        row = vllm.find_model_row(models_payload, model_id)
        if row is None:
            return None
        deployment = vllm.parse_models_row(row, provider_id=provider_id, api_base=base)
        version = await vllm.fetch_version(client, base)
        endpoint = vllm.build_endpoint_capabilities(provider_id, base, model_id, version=version)
        return endpoint, deployment
    except Exception as exc:  # noqa: BLE001 - best-effort, mirrors _discover_hot_models
        logger.debug("alcf: per-job vLLM probe failed for %s: %s", base, exc)
        return None


__all__ = [
    "FRAMEWORK_TASKS",
    "gateway_task_fact",
    "gateway_row_identity",
    "job_endpoint_url",
    "parse_gateway_model_row",
    "probe_job_endpoint",
]
