"""OpenRouter dialect adapter (model-capabilities brief Part 6, OpenRouter section).

One ``GET /api/v1/models`` row carries model AND per-route deployment facts
together:

* [M] ``context_length`` / ``architecture.input_modalities``; the model type
  from ``architecture.output_modalities`` (:func:`model_type_from_output_modalities`).
* [D] ``top_provider.context_length`` / ``top_provider.max_completion_tokens``
  (what the CURRENTLY ROUTED upstream actually serves -- may be smaller than
  the model's own ``context_length``).
* [D] ``supported_parameters`` -> ``route_params`` directly (brief: "gives
  route_params directly" -- no dialect table needed, unlike llama.cpp/vLLM).

When an optional parameter is sent, the request builder (Part 7 / P5) must set
``provider: {"require_parameters": true}`` so OpenRouter refuses to silently
route to an upstream that would drop it; :data:`REQUIRE_PARAMETERS_FLAG` names
that flag so P5 does not have to hand-roll the OpenRouter-specific spelling.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    model_type_fact,
    unknown,
)

DIALECT = "openrouter"

#: The flag OpenRouter defines for "only route to upstreams that support every
#: parameter this request sends" (brief: sent whenever an optional param is
#: used). A P5 request builder reads this constant rather than re-deriving the
#: OpenRouter-specific spelling ``{"provider": {"require_parameters": true}}``.
REQUIRE_PARAMETERS_FLAG: dict[str, dict[str, bool]] = {"provider": {"require_parameters": True}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def model_type_from_output_modalities(output: object) -> str | None:
    """The model type an OpenRouter ``architecture.output_modalities`` list proves.

    ``/api/v1/models`` is OpenRouter's chat-completions catalog (embedding models
    are listed separately), so a text output is a chat model. An output without
    text names the type directly: image output is image generation, audio output
    is speech. A missing list decides nothing.
    """
    if not isinstance(output, list):
        return None
    values = {str(value).strip().lower() for value in output}
    if "text" in values:
        return "chat"
    if "image" in values:
        return "image_generation"
    if "audio" in values:
        return "audio_speech"
    return None


def parse_model_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> tuple[ModelCapabilities, DeploymentCapabilities]:
    """Build one ``(ModelCapabilities, DeploymentCapabilities)`` pair from an OpenRouter model row."""
    observed_at = observed_at or _now_iso()
    model_id = str(row.get("id") or "")
    context_length = _positive_int(row.get("context_length"))
    architecture = row.get("architecture")
    input_modalities = architecture.get("input_modalities") if isinstance(architecture, Mapping) else None
    modalities_known = isinstance(input_modalities, list)

    top_provider = row.get("top_provider")
    top: Mapping[str, Any] = top_provider if isinstance(top_provider, Mapping) else {}
    served_context = _positive_int(top.get("context_length"))
    served_output = _positive_int(top.get("max_completion_tokens"))

    supported_params = row.get("supported_parameters")
    route_params_known = isinstance(supported_params, list)
    route_params: frozenset[str] = (
        frozenset(str(p) for p in supported_params) if isinstance(supported_params, list) else frozenset()
    )

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
    model_key = model_key_fact.value or model_id

    output_modalities = architecture.get("output_modalities") if isinstance(architecture, Mapping) else None
    model = ModelCapabilities(
        model_key=model_key,
        model_type=model_type_fact(
            model_type_from_output_modalities(output_modalities),
            source="openrouter",
            observed_at=observed_at,
            detail=f"openrouter /api/v1/models architecture.output_modalities={output_modalities!r}",
        ),
        context_max=(
            Fact(context_length, "openrouter", observed_at, "openrouter /api/v1/models context_length")
            if context_length is not None
            else unknown()
        ),
        input_modalities=(
            Fact(
                modalities_from_capabilities(input_modalities),
                "openrouter",
                observed_at,
                "openrouter /api/v1/models architecture.input_modalities",
            )
            if modalities_known
            else unknown()
        ),
    )
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(served_context, "server_report", observed_at, "openrouter top_provider.context_length")
            if served_context is not None
            else unknown()
        ),
        output_max=(
            Fact(
                served_output, "server_report", observed_at, "openrouter top_provider.max_completion_tokens"
            )
            if served_output is not None
            else unknown()
        ),
        route_params=(
            Fact(route_params, "server_report", observed_at, "openrouter supported_parameters")
            if route_params_known
            else unknown()
        ),
        fingerprint=(f"openrouter:route_params={sorted(route_params)}" if route_params_known else ""),
    )
    return model, deployment


__all__ = ["DIALECT", "REQUIRE_PARAMETERS_FLAG", "model_type_from_output_modalities", "parse_model_row"]
