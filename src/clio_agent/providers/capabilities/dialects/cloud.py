"""Cloud provider dialect defaults (model-capabilities brief Part 6, cloud section).

NVIDIA NIM, Azure, Bedrock, Vertex, Gemini, OpenAI and Anthropic report little
or nothing about themselves: there is no ``/props``, no self-hosted admin
endpoint, nothing to probe. Their MODEL facts come from the community catalogs
(models.dev / LiteLLM map, brief 5.1 step 5) and their ENDPOINT accepted
parameters come from LiteLLM's ``get_supported_openai_params`` (brief 5.2 step
2, already handled generically by
:func:`clio_agent.providers.capabilities.endpoint.build_endpoint_capabilities`
for any dialect).

The one thing genuinely dialect-specific here is brief Part 4.2: a cloud
deployment has no vision projector, no template, no slot count to forget --
there is nothing at THIS layer that could narrow a model's own capabilities,
so those deployment facts are recorded as :func:`~clio_agent.providers.
capabilities.records.no_restriction` (``True``/"every modality", ``source=
"dialect"``) rather than left unknown. Leaving them unknown would make the
three-valued AND in :mod:`combine` treat an unprobed cloud endpoint the same
as a self-hosted one nobody has evidenced yet -- which is not the same fact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.model_facts import (
    ReleaseDate,
    release_from_text,
    release_from_unix,
)
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    no_restriction,
)

#: Dialect names this module applies to (brief Part 6's cloud list). Each
#: shares the SAME "no restriction at this layer" deployment shape; only their
#: ``EndpointCapabilities.dialect`` differs (already resolved per-provider by
#: :func:`clio_agent.providers.capabilities.endpoint.dialect_for_provider`).
CLOUD_DIALECTS: frozenset[str] = frozenset(
    {"nvidia_nim", "azure", "bedrock", "vertex_ai", "gemini", "openai", "anthropic"}
)

#: Every modality CLIO currently models. A cloud endpoint imposes no
#: modality restriction of its own (brief 4.2) -- what the MODEL actually
#: accepts still narrows this in :mod:`combine`._intersect_modalities.
_ALL_MODALITIES: frozenset[str] = frozenset({"text", "image", "pdf", "audio", "video"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_deployment_capabilities(provider_id: str, api_base: str, model_id: str) -> DeploymentCapabilities:
    """A cloud deployment: "no restriction" on every layer this dialect could narrow.

    ``context_served``/``output_max``/``slots``/``tools_enabled``/
    ``reasoning_enabled`` are left unknown deliberately (brief 4.2 only calls
    for ``no_restriction`` where a boolean/set genuinely cannot restrict
    anything -- a numeric ceiling has no "no restriction" value, so those stay
    honestly unknown and the model's own ``context_max``/``output_max`` from
    the community catalogs stand alone in :func:`combine.combine_capabilities`'s
    ``_min_known`` -- "only model known").
    """
    observed_at = _now_iso()
    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
    return DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        modalities_enabled=no_restriction(
            _ALL_MODALITIES, observed_at=observed_at, detail="cloud endpoint: no modality projector to forget"
        ),
        tools_enabled=no_restriction(
            True, observed_at=observed_at, detail="cloud endpoint: no local tool-parser to disable"
        ),
        fingerprint="",  # brief 5.6: cloud invalidation is the CATALOG version, not this record
    )


#: The ``/models`` row field that states WHEN a cloud model was created, per
#: dialect, with its encoding: OpenAI's ``created`` (unix seconds) and
#: Anthropic's ``created_at`` (RFC 3339). Every other dialect's rows either
#: carry no such field or one that means something else (a server's own clock),
#: so they state no release date.
RELEASE_FIELDS: dict[str, tuple[str, Callable[[Any], ReleaseDate | None]]] = {
    "openai": ("created", release_from_unix),
    "anthropic": ("created_at", release_from_text),
}


def model_row_facts(dialect: str, row: Mapping[str, Any], *, observed_at: str) -> dict[str, Fact]:
    """``released_at`` / ``description`` from one cloud ``/models`` row, when it states them.

    The release date comes only from the dialect's own creation field
    (:data:`RELEASE_FIELDS`); ``description`` from a row that carries one
    (Gemini's model list does).
    """
    facts: dict[str, Fact] = {}
    spec = RELEASE_FIELDS.get(dialect)
    if spec is not None:
        field_name, parse = spec
        released = parse(row.get(field_name))
        if released is not None:
            facts["released_at"] = Fact(
                released,
                "server_report",
                observed_at,
                f"{dialect} /models {field_name}={row.get(field_name)!r}",
            )
    description = row.get("description")
    if isinstance(description, str) and description.strip():
        facts["description"] = Fact(
            description, "server_report", observed_at, f"{dialect} /models description"
        )
    return facts


__all__ = ["CLOUD_DIALECTS", "RELEASE_FIELDS", "build_deployment_capabilities", "model_row_facts"]
