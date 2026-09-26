"""OpenRouter dialect adapter (model-capabilities brief Part 6, OpenRouter section).

OpenRouter is a "golden" provider: its own ``GET /api/v1/models`` row is the
source of truth for its models, and ``handshake.base`` ranks this adapter's
facts above the overlay (``_AUTHORITATIVE_DIALECTS``). The listing is fetched
with :data:`MODELS_QUERY` (``?output_modalities=all``) -- the default hides
every model whose output is not text (image/video generation, embeddings,
speech, transcription, rerank, decisions), which must still be listed as what
they are.

One row carries model AND per-route deployment facts together:

* [M] ``architecture.input_modalities`` -> input modalities (``file`` is a
  document attachment -> ``pdf``); ``architecture.output_modalities`` -> output
  modalities and the task (:func:`task_from_output_modalities`).
* [M] ``context_length``; ``supported_parameters`` -> tools (``tools`` /
  ``tool_choice``), thinking (``reasoning`` / ``include_reasoning`` /
  ``reasoning_effort``) and structured output (``structured_outputs`` /
  ``response_format``). An EMPTY parameter list (a router) states nothing, so
  those stay unknown.
* [D] ``top_provider.context_length`` / ``max_completion_tokens`` (what the
  currently routed upstream serves); ``supported_parameters`` -> ``route_params``
  (the accepted-parameter set); ``pricing`` -> the per-token prices, ``free``
  (both exactly 0 -- never inferred from a ``:free`` suffix), with OpenRouter's
  ``-1`` recorded as ``"variable"``, never 0; the ``openrouter/`` author
  namespace -> ``router`` (OpenRouter's own meta-models: ``auto``, ``free``,
  ``fusion``, ...). The ``Router`` tokenizer is NOT used: it also tags the
  ``~vendor/*-latest`` version aliases, which point at one model.

When an optional parameter is sent, the request builder (Part 7 / P5) must set
``provider: {"require_parameters": true}`` so OpenRouter refuses to silently
route to an upstream that would drop it; :data:`REQUIRE_PARAMETERS_FLAG` names
that flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    modalities_from_capabilities,
    task_fact,
    unknown,
)

DIALECT = "openrouter"

#: Query string for the model listing: every output modality, not just text.
MODELS_QUERY = "output_modalities=all"

#: The flag OpenRouter defines for "only route to upstreams that support every
#: parameter this request sends" (brief: sent whenever an optional param is
#: used). A P5 request builder reads this constant rather than re-deriving the
#: OpenRouter-specific spelling ``{"provider": {"require_parameters": true}}``.
REQUIRE_PARAMETERS_FLAG: dict[str, dict[str, bool]] = {"provider": {"require_parameters": True}}

#: OpenRouter ``output_modalities`` -> task (``records.TASKS``), in priority
#: order: a model that outputs text is a text-generation model even if it also
#: outputs images/audio. ``decisions`` (e.g. typesafe jev-*) is classification.
_OUTPUT_TASKS: tuple[tuple[str, str], ...] = (
    ("text", "text-generation"),
    ("decisions", "text-classification"),
    ("embeddings", "feature-extraction"),
    ("rerank", "text-ranking"),
    ("transcription", "automatic-speech-recognition"),
    ("speech", "text-to-speech"),
    ("image", "text-to-image"),
    ("video", "text-to-video"),
)

#: ``supported_parameters`` that evidence each capability.
_TOOL_PARAMS = frozenset({"tools", "tool_choice"})
_REASONING_PARAMS = frozenset({"reasoning", "include_reasoning", "reasoning_effort"})
_STRUCTURED_PARAMS = frozenset({"structured_outputs", "response_format"})

#: OpenRouter's unified ``reasoning.effort`` levels.
_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: The author namespace of OpenRouter's own meta-models (routers that pick a
#: model per request).
_ROUTER_AUTHOR = "openrouter"

#: OpenRouter's price for "depends on the routed model".
_VARIABLE_PRICE = "-1"
VARIABLE = "variable"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def models_url(api_base: str) -> str:
    """The OpenRouter listing URL, asking for every output modality."""
    return f"{api_base.rstrip('/')}/models?{MODELS_QUERY}"


def task_from_output_modalities(output: object) -> str | None:
    """The task an OpenRouter ``architecture.output_modalities`` list proves."""
    if not isinstance(output, list):
        return None
    values = {str(value).strip().lower() for value in output}
    for modality, task in _OUTPUT_TASKS:
        if modality in values:
            return task
    return None


def _price(value: Any) -> str | None:
    """One price as OpenRouter states it: a decimal string, ``"variable"``, or None."""
    text = str(value).strip() if isinstance(value, (str, int, float)) else ""
    if not text:
        return None
    if text == _VARIABLE_PRICE:
        return VARIABLE
    try:
        Decimal(text)
    except InvalidOperation:
        return None
    return text


def _is_zero(price: str | None) -> bool:
    return price is not None and price != VARIABLE and Decimal(price) == 0


def _pricing_facts(row: Mapping[str, Any], observed_at: str) -> tuple[Fact, Fact]:
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return unknown("openrouter row has no pricing"), unknown("openrouter row has no pricing")
    prompt = _price(pricing.get("prompt"))
    completion = _price(pricing.get("completion"))
    if prompt is None or completion is None:
        return unknown("openrouter pricing incomplete"), unknown("openrouter pricing incomplete")
    detail = f"openrouter pricing prompt={pricing.get('prompt')!r} completion={pricing.get('completion')!r}"
    return (
        Fact({"prompt": prompt, "completion": completion}, "server_report", observed_at, detail),
        Fact(_is_zero(prompt) and _is_zero(completion), "server_report", observed_at, detail),
    )


def _param_fact(
    params: frozenset[str] | None, evidence: frozenset[str], observed_at: str, what: str
) -> Fact:
    if not params:
        return unknown(f"openrouter supported_parameters empty: {what} unstated")
    return Fact(
        bool(params & evidence),
        "openrouter",
        observed_at,
        f"openrouter supported_parameters ({what})",
    )


def _thinking_fact(params: frozenset[str] | None, observed_at: str) -> Fact:
    if not params:
        return unknown("openrouter supported_parameters empty: reasoning unstated")
    detail = "openrouter supported_parameters (reasoning)"
    if params & _REASONING_PARAMS:
        spec = ThinkingSpec(
            mechanism="effort_levels",
            levels=_EFFORT_LEVELS,
            effort_by_level={level: level for level in _EFFORT_LEVELS},
        )
        return Fact(spec, "openrouter", observed_at, detail)
    return Fact(ThinkingSpec(mechanism="none"), "openrouter", observed_at, detail)


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
    arch: Mapping[str, Any] = architecture if isinstance(architecture, Mapping) else {}
    input_modalities = arch.get("input_modalities")
    output_modalities = arch.get("output_modalities")

    top_provider = row.get("top_provider")
    top: Mapping[str, Any] = top_provider if isinstance(top_provider, Mapping) else {}
    served_context = _positive_int(top.get("context_length"))
    served_output = _positive_int(top.get("max_completion_tokens"))

    supported_params = row.get("supported_parameters")
    route_params_known = isinstance(supported_params, list)
    route_params: frozenset[str] = (
        frozenset(str(p) for p in supported_params)
        if isinstance(supported_params, list)
        else frozenset()
    )
    params = route_params if route_params_known else None

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
    model_key = model_key_fact.value or model_id
    pricing, free = _pricing_facts(row, observed_at)
    author = model_id.split("/", 1)[0] if "/" in model_id else ""

    model = ModelCapabilities(
        model_key=model_key,
        task=task_fact(
            task_from_output_modalities(output_modalities),
            source="openrouter",
            observed_at=observed_at,
            detail=f"openrouter architecture.output_modalities={output_modalities!r}",
        ),
        context_max=(
            Fact(
                context_length,
                "openrouter",
                observed_at,
                "openrouter /api/v1/models context_length",
            )
            if context_length is not None
            else unknown()
        ),
        input_modalities=(
            Fact(
                modalities_from_capabilities(input_modalities),
                "openrouter",
                observed_at,
                "openrouter architecture.input_modalities",
            )
            if isinstance(input_modalities, list)
            else unknown()
        ),
        output_modalities=(
            Fact(
                frozenset(str(m).strip().lower() for m in output_modalities),
                "openrouter",
                observed_at,
                "openrouter architecture.output_modalities",
            )
            if isinstance(output_modalities, list)
            else unknown()
        ),
        tools=_param_fact(params, _TOOL_PARAMS, observed_at, "tools"),
        structured_output=_param_fact(params, _STRUCTURED_PARAMS, observed_at, "structured output"),
        thinking=_thinking_fact(params, observed_at),
    )
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(
                served_context,
                "server_report",
                observed_at,
                "openrouter top_provider.context_length",
            )
            if served_context is not None
            else unknown()
        ),
        output_max=(
            Fact(
                served_output,
                "server_report",
                observed_at,
                "openrouter top_provider.max_completion_tokens",
            )
            if served_output is not None
            else unknown()
        ),
        route_params=(
            Fact(route_params, "server_report", observed_at, "openrouter supported_parameters")
            if route_params_known
            else unknown()
        ),
        pricing=pricing,
        free=free,
        router=(
            Fact(
                author == _ROUTER_AUTHOR,
                "server_report",
                observed_at,
                f"openrouter model author={author!r}",
            )
            if author
            else unknown("openrouter row id names no author")
        ),
        fingerprint=(
            f"openrouter:route_params={sorted(route_params)}" if route_params_known else ""
        ),
    )
    return model, deployment


__all__ = [
    "DIALECT",
    "MODELS_QUERY",
    "REQUIRE_PARAMETERS_FLAG",
    "VARIABLE",
    "models_url",
    "parse_model_row",
    "task_from_output_modalities",
]
