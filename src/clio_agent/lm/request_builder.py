"""The request builder (model-capabilities brief Part 7 / campaign slice P5).

Replaces ``lm/factory.py``'s old ``_provider_lm_kwargs``/``_thinking_kwargs``:
:func:`build_request_kwargs` is now the ONE place that turns an
:class:`~clio_agent.config.LMProviderConfig` into the kwargs
:func:`clio_agent.lm.factory._construct_lm` hands to ``dspy.LM``. Its ONLY
inputs are:

* the effective capabilities for ``(provider_id, api_base, model)``
  (:func:`clio_agent.providers.capabilities.accessor.get_effective_capabilities`
  -- the P4a/P4b model+endpoint+deployment combination), and
* the endpoint's dialect (:mod:`clio_agent.providers.capabilities.endpoint`),
  and
* the user's own settings (``config.temperature``/``top_p``/``top_k``/
  ``min_p``/``presence_penalty``/``thinking_level``/``thinking_budget``/
  ``provider_options``).

No per-model knowledge lives here -- only the generic "gate on the effective
parameter set, then let the dialect decide placement/spelling" rule (Part 7
items 1-6) and the CLI/SDK-transport delegation described below.

**Sampling is omitted by default** (item 1): a field is sent only when the
user set it, or the model's recommended sampling for the CURRENT thinking
mode (:attr:`EffectiveCapabilities.sampling_thinking`/``sampling_instruct``)
names a value -- either way, gated on the effective accepted-parameter set
(item 2/3). ``LMProviderConfig.temperature`` defaults to ``None`` (config.py);
with no value, ``dspy.LM``/LiteLLM omit the field entirely rather than
sending a stray ``0.0`` (verified: ``dspy.LM.__init__``'s own
``temperature: float | None = None`` already tolerates this).

**The accepted-parameter set is computed local-first, accessor-refined.**
:func:`clio_agent.providers.capabilities.endpoint.resolve_accepted_params` is
a pure, network-free lookup (LiteLLM's own ``get_supported_openai_params`` +
the dialect supplement table) -- it needs no prior handshake to answer
correctly for the dialects it knows. This module always computes that base
answer FIRST, then prefers the richer, handshake-derived
``EffectiveCapabilities.accepted_params`` (which additionally intersects
per-route ``route_params`` and subtracts a model's ``forbidden_params``) when
a handshake has actually run and populated one. This is deliberate: the CLI
boot path (``setup_dspy`` -> ``load_config_from_env`` -> ``create_lm``, RULE 2's
smoke test) and any bare ``LMProviderConfig()`` + ``create_lm()`` construction
(most unit tests) never run a handshake at all, and "fail closed" must never
mean "an operator's own locally-configured backend goes silently mute before
its first handshake" -- see the design note under :data:`_SDK_ONLY_DIALECTS`
for the one place this still needs a further, deliberate exemption.

**Thinking is a split table, not one unified mapping** (item 5, dialect
table). ``llama_cpp``/``vllm``/``ollama``/``lm_studio``/``openrouter`` --
the dialects the P4a/P4b capability-record adapters actually populate a
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec` for -- are
driven by :mod:`clio_agent.lm.dialect_wire`, itself driven by the model's own
``ThinkingSpec`` and the control :mod:`.combine` chose (Part 5.5). ``codex``/
``claude_code``/``anthropic``/``openai`` keep the EXISTING, live-verified
SDK/CLI-transport mapping in :mod:`clio_agent.providers.thinking`
(``resolve_thinking``) unchanged: those four are not HTTP dialects with a
JSON request body llama.cpp-style parameter gating applies to (codex/
claude_code are Python SDK sessions; anthropic/openai are real cloud APIs
whose thinking shape LiteLLM already translates end-to-end), and no P4a/P4b
adapter builds a ``ThinkingSpec``/``ThinkingDecision`` for them yet -- unifying
that is future work (a codex/claude_code dialect adapter), not this slice.
This split is a deliberate, documented scoping call, not an oversight.

**``providers/thinking.py``'s ``ACCEPTED_LEVELS`` keeps its ``lm_studio``/
``ollama``/``argonne`` rows.** This module never calls ``resolve_thinking``
for those three dialects any more (they go through :mod:`.dialect_wire`
instead, above) -- but ``providers/reasoning_levels.py``'s ``model_reasoning``
(the UI catalog's ``reasoning.levels`` display, an unrelated consumer, brief
Part 7's UI deliverable) still reads those same rows to decide which levels
the model picker advertises for a local model. Deleting them would silently
empty that picker with no replacement in this slice's scope, for zero benefit
to request-building. Folding the picker's OWN level list onto the model's
``ThinkingSpec``/effective ``ThinkingDecision`` (so it, too, stops being
kind-keyed) is future work, tracked with the codex/claude_code dialect
adapter above -- not deleted here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.lm import dialect_wire
from clio_agent.providers.capabilities import endpoint as capability_endpoint
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.combine import EffectiveCapabilities

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.config import LMProviderConfig

logger = logging.getLogger(__name__)

#: The two CLI/SDK transports LiteLLM has no real provider translator for at
#: all (``providers.codex_litellm``/``providers.claude_code_litellm`` own
#: ``CustomLLM`` classes that read a small, fixed set of ``optional_params``
#: directly -- see ``lm/factory.py``'s ``_CUSTOM_TRANSPORT_PREFIXES``). Their
#: sampling surface is exempted from the generic accepted-parameter gate:
#: :func:`clio_agent.providers.capabilities.endpoint.resolve_accepted_params`
#: has neither a LiteLLM mapping nor a supplement row for ``"codex"``/
#: ``"claude_code"`` (there is no HTTP wire to describe), so gating them the
#: same way every other dialect is gated would silently mute an operator's
#: own configured sampling on every codex/claude_code turn -- the opposite of
#: "fail closed" (that phrase means "don't invent support we have no
#: evidence for", not "break a transport this slice has no evidence ABOUT").
#: Matches this pair's existing, unconditional treatment everywhere else in
#: the codebase (``_CUSTOM_TRANSPORT_PREFIXES``, ``parse_retry_capability``).
_SDK_ONLY_DIALECTS: frozenset[str] = frozenset({"codex", "claude_code"})

#: Dialects whose thinking config still goes through the existing
#: ``providers.thinking.resolve_thinking`` engine (see module docstring).
_LEGACY_THINKING_DIALECTS: frozenset[str] = frozenset(
    {"codex", "claude_code", "anthropic", "openai"}
)

#: User-settable optional sampling fields (beyond temperature, handled
#: separately since its default/recommended-value rule differs slightly --
#: item 1 vs. item 2). Keyed by clio's own internal field name; each is
#: gated on the effective parameter set and placed via
#: :func:`clio_agent.lm.dialect_wire.place_optional_param`.
_OPTIONAL_SAMPLING_FIELDS: tuple[str, ...] = (
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
)


def _dialect_and_litellm_prefix(config: "LMProviderConfig") -> tuple[str, str]:
    """Return ``(dialect, litellm_prefix)`` for ``config``'s catalog preset."""

    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider_id = getattr(config, "provider_id", "") or str(config.provider)
    preset = get_provider(provider_id)
    litellm_prefix = preset.litellm_prefix if preset is not None else str(config.provider)
    dialect = capability_endpoint.dialect_for_provider(
        str(config.provider), litellm_prefix, provider_id
    )
    return dialect, litellm_prefix


def _resolve(
    config: "LMProviderConfig",
) -> tuple[str, frozenset[str], EffectiveCapabilities]:
    """Return ``(dialect, accepted_params, effective_capabilities)`` for ``config``.

    See the module docstring's "computed local-first, accessor-refined" note.
    """

    dialect, litellm_prefix = _dialect_and_litellm_prefix(config)
    base_fact = capability_endpoint.resolve_accepted_params(
        dialect, config.model, custom_llm_provider=litellm_prefix
    )
    base_accepted: frozenset[str] = (
        base_fact.value if base_fact.known and base_fact.value else frozenset()
    )

    provider_id = getattr(config, "provider_id", "") or str(config.provider)
    api_base = getattr(config, "api_base", "") or ""
    effective = get_effective_capabilities(provider_id, api_base, config.model)
    accepted: frozenset[str] = (
        effective.accepted_params.value
        if effective.accepted_params.known and effective.accepted_params.value is not None
        else base_accepted
    )
    return dialect, accepted, effective


def _thinking_requested_on(config: "LMProviderConfig", effective: EffectiveCapabilities) -> bool:
    """Whether thinking is switched ON for this request (which sampling mode applies)."""

    level = getattr(config, "thinking_level", None)
    if level not in (None, "off"):
        return True
    spec = effective.thinking.spec
    return spec is not None and spec.mechanism == "always_on"


def _recommended_sampling(effective: EffectiveCapabilities, thinking_on: bool) -> dict[str, float]:
    decision = effective.sampling_thinking if thinking_on else effective.sampling_instruct
    return dict(decision.value) if decision.known and decision.value else {}


def _lm_studio_allowed_options(config: "LMProviderConfig") -> tuple[str, ...] | None:
    """LM Studio's own ``reasoning.allowed_options`` for this exact deployment, if known."""

    from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415
    from clio_agent.providers.identity import deployment_key  # noqa: PLC0415

    provider_id = getattr(config, "provider_id", "") or str(config.provider)
    api_base = getattr(config, "api_base", "") or ""
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(provider_id, api_base, config.model)
    )
    if deployment is None or not deployment.template_caps.known:
        return None
    raw = (deployment.template_caps.value or {}).get("reasoning_allowed_options")
    if not isinstance(raw, list):
        return None
    return tuple(str(v) for v in raw)


def _legacy_thinking_kwargs(config: "LMProviderConfig") -> dict[str, Any]:
    """Codex/claude_code/anthropic/openai thinking, via the existing SDK/CLI mapping.

    Unchanged behavior from the pre-P5 ``_thinking_kwargs`` (module docstring
    explains why these four stay on :func:`~clio_agent.providers.thinking.
    resolve_thinking` rather than :mod:`clio_agent.lm.dialect_wire`).
    """

    from clio_agent.providers.reasoning_levels import model_effort_levels  # noqa: PLC0415
    from clio_agent.providers.thinking import (  # noqa: PLC0415
        log_unsupported_thinking,
        resolve_thinking,
    )

    plan = resolve_thinking(
        config.provider,
        getattr(config, "thinking_level", None),
        int(getattr(config, "thinking_budget", 0) or 0),
        effort_levels=model_effort_levels(config.provider, config.model or ""),
    )
    if not plan.supported:
        log_unsupported_thinking(plan)
        return {}
    extras = dict(plan.litellm_kwargs)
    if plan.sdk_thinking is not None:
        extras["claude_code_thinking"] = plan.sdk_thinking
    return extras


def _stop_sequences() -> list[str]:
    """The DSPy trajectory-regurgitation stop list, or ``lm.stop_sequences``' override.

    Unchanged literal list / override mechanism from the pre-P5 code; the
    difference (Part 7 item 3) is purely in WHEN the caller sends this --
    only when ``"stop"`` is in the effective accepted-parameter set, never on
    a per-model reasoning-capability guess.
    """

    from clio_agent import conf  # noqa: PLC0415 - keep this module import-light

    raw_stop = conf.resolve("lm.stop_sequences", env="CLIO_LM_STOP_SEQUENCES", default=None)
    if isinstance(raw_stop, (list, tuple)):
        override = [str(s) for s in raw_stop if str(s)]
        if override:
            return override
    elif raw_stop:
        override = [s for s in str(raw_stop).split("||") if s]
        if override:
            return override
    return [
        "[[ ## observation",
        "[[ ## thought_",
        "[[ ## tool_name_",
        "[[ ## tool_args_",
    ]


def build_request_kwargs(
    config: "LMProviderConfig", *, role: Literal["main", "planner"] = "main"
) -> dict[str, Any]:
    """Build the LiteLLM/``dspy.LM`` kwargs for one request (Part 7).

    Args:
        config: The resolved provider config. Its own settings (temperature/
            top_p/top_k/min_p/presence_penalty/thinking_level/thinking_budget/
            provider_options) are the "user's settings" input this function
            takes; everything else comes from the effective capabilities.
        role: ``"main"`` uses ``config.temperature`` (an explicit user value,
            else the model's recommended sampling for the current thinking
            mode). ``"planner"`` uses ``config.planner_temperature``
            unconditionally (item 1: "planner and router determinism may set
            temperature explicitly") -- still gated on the effective
            parameter set either way.

    Returns:
        The kwargs dict to splat into ``dspy.LM(...)`` alongside ``model``/
        ``api_key``/``api_base``/``max_tokens``/``cache``. Always includes
        ``drop_params=True`` (item 9's safety net) unless the caller's own
        ``provider_options`` already set it.
    """

    dialect, accepted, effective = _resolve(config)
    sdk_only = dialect in _SDK_ONLY_DIALECTS
    extras: dict[str, Any] = dict(getattr(config, "provider_options", {}) or {})
    sent_optional = False

    thinking_on = _thinking_requested_on(config, effective)
    recommended = _recommended_sampling(effective, thinking_on)

    # -- temperature (item 1) ------------------------------------------
    temperature_candidate: float | None
    if role == "planner":
        temperature_candidate = config.planner_temperature
    else:
        temperature_candidate = (
            config.temperature if config.temperature is not None else recommended.get("temperature")
        )
    if temperature_candidate is not None and (sdk_only or "temperature" in accepted):
        extras["temperature"] = temperature_candidate
        sent_optional = True

    # -- the rest of the sampling surface (items 1-3) -------------------
    for field in _OPTIONAL_SAMPLING_FIELDS:
        user_value = getattr(config, field, None)
        candidate = user_value if user_value is not None else recommended.get(field)
        if candidate is None:
            continue
        if not sdk_only and field not in accepted:
            continue
        dialect_wire.place_optional_param(extras, dialect, field, candidate)
        sent_optional = True

    # -- thinking (item 4) ------------------------------------------------
    if dialect in _LEGACY_THINKING_DIALECTS:
        thinking_extras = _legacy_thinking_kwargs(config)
        if thinking_extras:
            extras.update(thinking_extras)
            sent_optional = True
    elif dialect in dialect_wire.HTTP_DIALECTS:
        allowed_options = _lm_studio_allowed_options(config) if dialect == "lm_studio" else None
        wire = dialect_wire.thinking_wire(
            dialect,
            effective.thinking,
            level=getattr(config, "thinking_level", None),
            lm_studio_allowed_options=allowed_options,
        )
        if wire:
            dialect_wire.apply_thinking_wire(extras, dialect, wire)
            sent_optional = True
    elif getattr(config, "thinking_level", None) not in (None, "off"):
        # No silent no-op (ground rule): a thinking level was explicitly
        # requested on a dialect this module has no mapping for at all
        # (neither the legacy SDK/CLI engine nor a dialect_wire entry).
        logger.warning(
            "thinking_unsupported provider=%s dialect=%s requested_level=%s "
            "reason=no_mapping_for_dialect",
            config.provider,
            dialect,
            config.thinking_level,
        )

    # -- stop sequences (item 3) -------------------------------------------
    # No `sdk_only` exemption here (unlike sampling above): nobody explicitly
    # configures a `stop` override on `LMProviderConfig`, so there is no
    # operator intent to preserve for codex/claude_code -- an unknown
    # accepted-set answer stays "don't send it".
    if "stop" not in extras and "stop" in accepted:
        extras["stop"] = _stop_sequences()
        sent_optional = True

    # OpenRouter: refuse to silently route around an optional param this
    # request just asked for (Part 7 item 3 / dialects.openrouter).
    if dialect == "openrouter" and sent_optional:
        dialect_wire.openrouter_require_parameters(extras)

    if config.provider == "codex":
        extras["codex_transport"] = config.codex_transport
    elif config.provider == "claude_code":
        extras["claude_code_transport"] = config.claude_code_transport

    # Safety net only (item 9): every optional field above is already gated
    # on the effective parameter set. `drop_params` is the backstop for when
    # that record is wrong, so a stale/incomplete capability record degrades
    # to "field silently omitted" instead of a hard request failure; P2's
    # `_warn_dropped_params` (factory.py, called from `_construct_lm`) turns
    # every actual drop into a logged bug signal instead of a silent one.
    extras.setdefault("drop_params", True)
    return extras


__all__ = ["build_request_kwargs"]
