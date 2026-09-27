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
* the user's own settings (``config.temperature`` and the rest of the
  sampling surface named by :data:`~clio_agent.providers.capabilities.
  accepted_parameters.TUNABLES`, ``thinking_level``/``thinking_budget``/
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
correctly for the dialects it knows. The richer, handshake-derived
``EffectiveCapabilities.accepted_params`` (which additionally intersects
per-route ``route_params`` and subtracts a model's ``forbidden_params``) wins
when a handshake has actually run and populated one. This is deliberate: the
CLI boot path (``setup_dspy`` -> ``load_config_from_env`` -> ``create_lm``,
RULE 2's smoke test) and any bare ``LMProviderConfig()`` + ``create_lm()``
construction (most unit tests) never run a handshake at all, and "fail closed"
must never mean "an operator's own locally-configured backend goes silently
mute before its first handshake". The rule lives in ONE place,
:func:`~clio_agent.providers.capabilities.accepted_parameters.accepted_param_set`,
which also feeds the catalog's ``accepted_parameters`` -- so the settings a
person is offered are exactly the settings this module sends.

**A saved setting the model does not accept is never sent**, and each such
drop is logged with a typed reason (``response_setting_not_sent``). The CLI/SDK
transports (codex, claude_code) accept none: their SDK options carry no
sampling field at all (claude-agent-sdk ``ClaudeAgentOptions``; openai-codex
``TurnStartParams``/the Responses body clio builds), so nothing is sent there
rather than being handed to a transport that silently ignores it.

**Thinking is ONE mapping, covering every dialect** (item 5, dialect table).
:func:`clio_agent.lm.dialect_wire.thinking_wire` builds the on/off/level
kwargs for every dialect this module configures an LM for -- the five HTTP
dialects (llama.cpp/vLLM/Ollama/LM Studio/OpenRouter) AND codex/claude_code/
anthropic/openai, all driven by the model's own
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec` and the
control :mod:`.combine` chose (Part 5.5). ``providers/thinking.py`` (the old
provider-name-keyed ``resolve_thinking``/``ACCEPTED_LEVELS`` engine) and
``providers/reasoning_levels.py`` (its catalog-display counterpart) are
deleted: codex's ``ThinkingSpec`` comes from its SDK's own reported
``supportedReasoningEfforts`` (``providers.capabilities.dialects.codex``),
claude_code's from the CLI's own ``supportedEffortLevels``
(``providers.capabilities.dialects.claude_code``), and anthropic/openai's
from LiteLLM's own introspection (``providers.capabilities.dialects.
cloud_thinking``) -- real per-model/per-provider data feeding the SAME
``ThinkingSpec`` shape every other dialect already uses, never a second,
provider-name-keyed mapping table. Only the CLIO-level vocabulary itself
(``off``/``low``/.../``ultra``, the generic budget ladder) survives, in
:mod:`clio_agent.providers.thinking_levels`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.lm import dialect_wire
from clio_agent.providers.capabilities import accepted_parameters
from clio_agent.providers.capabilities import endpoint as capability_endpoint
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.combine import EffectiveCapabilities

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.config import LMProviderConfig

logger = logging.getLogger(__name__)

#: User-settable optional request fields (beyond temperature, handled
#: separately since its default/recommended-value rule differs slightly --
#: item 1 vs. item 2). Keyed by clio's own internal field name (the
#: ``LMProviderConfig`` attribute); each is gated on the effective parameter
#: set via its :class:`~clio_agent.providers.capabilities.accepted_parameters.
#: Tunable` and placed via :func:`clio_agent.lm.dialect_wire.place_optional_param`.
#: ``max_tokens`` is carried by ``dspy.LM`` itself (``lm/factory.py``) and
#: ``parallel`` is a load setting, so neither is a request field here;
#: ``context_length`` is one only where its wire name (Ollama ``num_ctx``) is
#: accepted -- LM Studio takes it at load time instead.
_OPTIONAL_REQUEST_FIELDS: tuple[str, ...] = (
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "seed",
    "context_length",
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


def local_first_effective(
    provider_id: str, api_base: str, model_id: str, *, dialect: str, litellm_prefix: str
) -> EffectiveCapabilities:
    """The effective capabilities, with the thinking spec computed local-first.

    Before any handshake has recorded this deployment, a cloud dialect's thinking
    spec is still knowable without the network (anthropic/openai: LiteLLM's
    local map, :func:`~clio_agent.providers.capabilities.dialects.cloud_thinking.
    local_thinking_spec`) -- the same fact the handshake would record. Without
    this, a requested effort on a freshly bound cloud model was silently not
    applied ("not controllable here"). A recorded thinking fact always wins.
    """

    from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415
    from clio_agent.providers.capabilities.combine import combine_capabilities  # noqa: PLC0415
    from clio_agent.providers.capabilities.dialects import cloud_thinking  # noqa: PLC0415
    from clio_agent.providers.capabilities.records import ModelCapabilities  # noqa: PLC0415
    from clio_agent.providers.identity import deployment_key, endpoint_key  # noqa: PLC0415

    effective = get_effective_capabilities(provider_id, api_base, model_id)
    if effective.thinking.known:
        return effective
    spec = cloud_thinking.local_thinking_spec(dialect, model_id)
    if not spec.known:
        return effective
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(provider_id, api_base, model_id)
    )
    model_key = (
        deployment.model_key.value
        if deployment is not None and deployment.model_key.known and deployment.model_key.value
        else model_id
    )
    model = invalidation.get_model_capabilities(model_key) or ModelCapabilities(model_key=model_key)
    endpoint = invalidation.get_endpoint_capabilities(
        endpoint_key(provider_id, api_base)
    ) or capability_endpoint.build_endpoint_capabilities(
        provider_id, api_base, dialect, model_id, custom_llm_provider=litellm_prefix
    )
    return combine_capabilities(replace(model, thinking=spec), endpoint, deployment)


def _resolve(
    config: "LMProviderConfig",
) -> tuple[str, frozenset[str], EffectiveCapabilities]:
    """Return ``(dialect, accepted_params, effective_capabilities)`` for ``config``.

    See the module docstring's "computed local-first, accessor-refined" note.
    """

    dialect, litellm_prefix = _dialect_and_litellm_prefix(config)
    provider_id = getattr(config, "provider_id", "") or str(config.provider)
    api_base = getattr(config, "api_base", "") or ""
    effective = local_first_effective(
        provider_id, api_base, config.model, dialect=dialect, litellm_prefix=litellm_prefix
    )
    accepted, _facts = accepted_parameters.accepted_param_set(
        provider_id,
        api_base,
        config.model,
        dialect=dialect,
        litellm_prefix=litellm_prefix,
        effective=effective,
    )
    return dialect, accepted, effective


def _accepts(field: str, accepted: frozenset[str]) -> bool:
    spec = accepted_parameters.tunable(field)
    return spec is not None and accepted_parameters.is_accepted(spec, accepted)


def _log_not_sent(config: "LMProviderConfig", dialect: str, field: str) -> None:
    """A person's saved setting the model does not accept: kept, never sent, logged."""

    logger.warning(
        "response_setting_not_sent provider=%s dialect=%s model=%s field=%s "
        "reason=not_accepted_by_model",
        getattr(config, "provider_id", "") or config.provider,
        dialect,
        config.model,
        field,
    )


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
    if temperature_candidate is not None and _accepts("temperature", accepted):
        extras["temperature"] = temperature_candidate
        sent_optional = True
    elif role == "main" and config.temperature is not None:
        _log_not_sent(config, dialect, "temperature")

    # -- the rest of the request surface (items 1-3) ---------------------
    for field in _OPTIONAL_REQUEST_FIELDS:
        user_value = getattr(config, field, None)
        if field == "context_length" and not user_value:
            user_value = None  # 0 means "leave the server's own context alone"
        candidate = user_value if user_value is not None else recommended.get(field)
        if candidate is None:
            continue
        if not _accepts(field, accepted):
            if user_value is not None and role == "main":
                _log_not_sent(config, dialect, field)
            continue
        dialect_wire.place_optional_param(extras, dialect, field, candidate)
        sent_optional = True

    # -- thinking (item 4) ------------------------------------------------
    allowed_options = _lm_studio_allowed_options(config) if dialect == "lm_studio" else None
    wire = dialect_wire.thinking_wire(
        dialect,
        effective.thinking,
        level=getattr(config, "thinking_level", None),
        lm_studio_allowed_options=allowed_options,
        budget_tokens=int(getattr(config, "thinking_budget", 0) or 0),
    )
    if wire:
        dialect_wire.apply_thinking_wire(extras, dialect, wire)
        sent_optional = True
        if not effective.thinking.known:
            # Unknown is not unsupported: the level rides through untranslated
            # and the upstream decides -- recorded, never a silent pass-through.
            logger.info(
                "thinking_passthrough provider=%s dialect=%s requested_level=%s reason=%s",
                config.provider,
                dialect,
                config.thinking_level,
                dialect_wire.REASONING_LEVELS_UNKNOWN,
            )
    elif getattr(config, "thinking_level", None) not in (None, "off"):
        # No silent no-op (ground rule): a thinking level was explicitly
        # requested but nothing was sent -- either this dialect has no
        # mapping at all, or the model's own thinking evidence is unknown/
        # not controllable here (brief 5.5). Either way it is worth a
        # structured signal rather than a quiet no-op.
        logger.warning(
            "thinking_unsupported provider=%s dialect=%s requested_level=%s reason=%s",
            config.provider,
            dialect,
            config.thinking_level,
            "no_thinking_control"
            if effective.thinking.known
            else dialect_wire.REASONING_LEVELS_UNKNOWN,
        )

    # -- stop sequences (item 3) -------------------------------------------
    # An unknown accepted-set answer (codex/claude_code) stays "don't send it".
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
