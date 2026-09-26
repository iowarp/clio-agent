"""LM Studio dialect adapter (model-capabilities brief Part 6, LM Studio section).

LM Studio 0.4+ serves a newer ``GET /api/v1/models`` surface; older builds only
have ``GET /api/v0/models`` (P4a's :mod:`clio_agent.providers.handshake.lmstudio`
already reads that one). The two shapes differ:

* **v1** (this module's primary path): ``max_context_length`` / ``capabilities.
  vision`` / ``trained_for_tool_use`` / ``reasoning.allowed_options`` (the exact
  ``reasoning_effort`` values this server accepts for THIS model -- a
  deployment fact, not a model one) and a nested ``loaded_context_length``.
* **v0** (P4a's fallback): ``capabilities`` is a flat string list
  (``"tool_use"``, ``"vision"``) rather than an object.

The brief calls for the ``lmstudio`` client "if appropriate"; it isn't here --
that SDK talks LM Studio's own WebSocket control protocol, which cannot be
exercised against a recorded HTTP fixture for the contract tests this slice
requires (brief 9.2 names a plain ``LM Studio v1/v0`` HTTP contract test), and
the plain REST surface below is exactly what Part 6 names field-for-field. A
future slice can add the SDK path for the richer control-plane operations
(explicit load/unload) it uniquely offers; this module stays HTTP-only,
dialect-only knowledge.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.model_facts import (
    ParameterCount,
    parameters_from_size_field,
)
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    unknown,
)

DIALECT = "lm_studio"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _v1_vision(row: Mapping[str, Any]) -> bool | None:
    caps = row.get("capabilities")
    if isinstance(caps, Mapping) and isinstance(caps.get("vision"), bool):
        return bool(caps["vision"])
    if isinstance(caps, list):  # a v1 server that still reports the v0 list shape
        return "vision" in caps
    return None


def _v1_allowed_reasoning_options(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    reasoning = row.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return None
    options = reasoning.get("allowed_options")
    if not isinstance(options, list):
        return None
    return tuple(str(o) for o in options)


def _v1_parameters(row: Mapping[str, Any], observed_at: str) -> Fact[ParameterCount]:
    """``params_string`` (LM Studio's rounded display size, e.g. ``'8B'``), or unknown."""
    total = parameters_from_size_field(row.get("params_string"))
    if total is None:
        return unknown()
    return Fact(
        ParameterCount(total=total, precision="rounded"),
        "server_report",
        observed_at,
        f"lmstudio /api/v1/models params_string={row.get('params_string')!r}",
    )


def parse_v1_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> tuple[ModelCapabilities, DeploymentCapabilities]:
    """Build one ``(ModelCapabilities, DeploymentCapabilities)`` pair from a v1 ``/api/v1/models`` row."""
    observed_at = observed_at or _now_iso()
    model_id = str(row.get("id") or row.get("key") or "")
    max_context = _positive_int(row.get("max_context_length"))
    loaded_context = _positive_int(row.get("loaded_context_length"))
    vision = _v1_vision(row)
    trained_for_tool_use = row.get("trained_for_tool_use")
    tools_known = isinstance(trained_for_tool_use, bool)
    allowed_options = _v1_allowed_reasoning_options(row)

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
    model_key = model_key_fact.value or model_id

    model = ModelCapabilities(
        model_key=model_key,
        context_max=(
            Fact(max_context, "server_report", observed_at, "lmstudio /api/v1/models max_context_length")
            if max_context is not None
            else unknown()
        ),
        tools=(
            Fact(
                bool(trained_for_tool_use),
                "server_report",
                observed_at,
                "lmstudio /api/v1/models trained_for_tool_use",
            )
            if tools_known
            else unknown()
        ),
        input_modalities=(
            Fact(
                frozenset({"text", "image"}) if vision else frozenset({"text"}),
                "server_report",
                observed_at,
                "lmstudio /api/v1/models capabilities.vision",
            )
            if vision is not None
            else unknown()
        ),
        parameters=_v1_parameters(row, observed_at),
        description=(
            Fact(row["description"], "server_report", observed_at, "lmstudio /api/v1/models description")
            if isinstance(row.get("description"), str) and row["description"].strip()
            else unknown()
        ),
    )
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(loaded_context, "server_report", observed_at, "lmstudio /api/v1/models loaded_context_length")
            if loaded_context is not None
            else unknown()
        ),
        # brief 5.6: model key + loaded_context_length is the deployment fingerprint.
        fingerprint=(f"lm_studio:model={model_id}:loaded_context={loaded_context}" if model_id else ""),
    )
    if allowed_options is not None:
        deployment = _with_allowed_reasoning_options(deployment, allowed_options, observed_at)
    return model, deployment


def _with_allowed_reasoning_options(
    deployment: DeploymentCapabilities, allowed_options: tuple[str, ...], observed_at: str
) -> DeploymentCapabilities:
    """Stash ``reasoning.allowed_options`` on ``template_caps`` (the generic deployment-detail bag).

    There is no dedicated ``DeploymentCapabilities`` field for "the exact
    reasoning_effort values this server accepts for this model" -- it is
    LM-Studio-specific detail, not a cross-dialect concept like
    ``chat_template_caps``. ``template_caps`` already exists precisely to carry
    a dialect's own verbatim capability blob through to a caller that knows
    how to read it (the thinking-mapping request builder, Part 7).
    """
    from dataclasses import replace  # noqa: PLC0415

    return replace(
        deployment,
        template_caps=Fact(
            {"reasoning_allowed_options": list(allowed_options)},
            "server_report",
            observed_at,
            "lmstudio /api/v1/models reasoning.allowed_options",
        ),
    )


def parse_v0_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> tuple[ModelCapabilities, DeploymentCapabilities]:
    """Build one ``(ModelCapabilities, DeploymentCapabilities)`` pair from a v0 ``/api/v0/models`` row.

    The fallback for a pre-0.4 LM Studio build: ``max_context_length`` ->
    ceiling, ``loaded_context_length`` -> the runtime window,
    ``quantization``/``arch``/``state`` pass through as raw identity metadata,
    a flat ``capabilities`` list (``"tool_use"``, ``"vision"``) -> tools/
    input_modalities, and ``state == "loaded"`` sets ``is_loaded``.
    """
    observed_at = observed_at or _now_iso()
    capabilities = row.get("capabilities") or []
    if not isinstance(capabilities, list):
        capabilities = []
    caps = tuple(str(cap) for cap in capabilities)
    model_id = str(row.get("id", ""))
    context_max = _positive_int(row.get("max_context_length"))
    capabilities_known = bool(caps)
    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
    model_key = model_key_fact.value or model_id

    model = ModelCapabilities(
        model_key=model_key,
        context_max=(
            Fact(context_max, "server_report", observed_at, "lmstudio /api/v0/models max_context_length")
            if context_max is not None
            else unknown()
        ),
        tools=(
            Fact("tool_use" in caps, "server_report", observed_at, "lmstudio /api/v0/models capabilities")
            if capabilities_known
            else unknown()
        ),
        input_modalities=(
            Fact(
                modalities_from_capabilities(caps),
                "server_report",
                observed_at,
                "lmstudio /api/v0/models capabilities",
            )
            if capabilities_known
            else unknown()
        ),
    )
    loaded_context = _positive_int(row.get("loaded_context_length"))
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(
                loaded_context, "server_report", observed_at, "lmstudio /api/v0/models loaded_context_length"
            )
            if loaded_context is not None
            else unknown()
        ),
    )
    return model, deployment


def build_endpoint_capabilities(provider_id: str, api_base: str, model_id: str) -> EndpointCapabilities:
    """Build this endpoint's :class:`EndpointCapabilities`.

    LM Studio reports no dedicated version string on either models endpoint at
    time of writing, so ``server_version``/``fingerprint`` stay unknown/empty
    here -- an honest "we don't know", never a guessed value. The supplement
    table (brief 5.2 step 3) still removes ``chat_template_kwargs`` from the
    accepted-parameter set (LM Studio must never receive it).
    """
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415

    return capability_endpoint.build_endpoint_capabilities(
        provider_id,
        api_base,
        DIALECT,
        model_id,
        custom_llm_provider="lm_studio",
    )


async def fetch_rows(client: Any, api_base: str) -> tuple[list[dict[str, Any]], str]:
    """``GET /api/v1/models``, falling back to ``GET /api/v0/models`` (brief Part 6).

    Returns ``(rows, schema)`` where ``schema`` is ``"v1"`` or ``"v0"`` so the
    caller knows which parser applies -- :func:`parse_v1_row` for ``"v1"``,
    :func:`parse_v0_row` for ``"v0"``. Both endpoints live at the NATIVE root
    (no ``/v1`` API-base suffix). An empty ``rows``/``schema=""`` means
    neither endpoint answered.
    """
    from clio_agent.providers.api_base import native_root  # noqa: PLC0415

    root = native_root(api_base)
    for path, schema in ((f"{root}/api/v1/models", "v1"), (f"{root}/api/v0/models", "v0")):
        try:
            response = await client.get(path)
        except Exception:  # noqa: BLE001 - try the next schema / report no rows
            continue
        if response.status_code >= 400:
            continue
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if schema == "v1" and not isinstance(rows, list):
            rows = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)], schema
    return [], ""


__all__ = [
    "DIALECT",
    "build_endpoint_capabilities",
    "fetch_rows",
    "parse_v0_row",
    "parse_v1_row",
]
