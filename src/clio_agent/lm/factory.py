"""LM construction/factory layer.

Extracted from :mod:`clio_agent.config` (#769). ``clio_agent.config`` re-exports
every public name here so historical import seams (and their monkeypatch points)
keep working; new code should import from :mod:`clio_agent.lm.factory` directly.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    import dspy

    from clio_agent.config import LMProviderConfig

from clio_agent.lm.io_logging import _io_logging_lm_cls
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.codex.constants import LITELLM_PROVIDER as _CODEX_LITELLM_PREFIX
from clio_agent.providers.codex.constants import (
    LITELLM_PROVIDER_SDK as _CODEX_LITELLM_PREFIX_SDK,
)
from clio_agent.runtime import turn_lm_ledger

_dspy_cache = None
logger = logging.getLogger(__name__)


def _defer_tiktoken_enabled() -> bool:
    """Whether to defer litellm's eager ~40 MB cl100k_base load (``lm.defer_tiktoken``).

    Default on; a pure lazy-loading optimisation (see :mod:`clio_agent.lm.lazy_tiktoken`).
    Operators can disable it with ``lm.defer_tiktoken`` / ``CLIO_LM_DEFER_TIKTOKEN``.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "lm.defer_tiktoken", env="CLIO_LM_DEFER_TIKTOKEN", default=True, cast=conf.as_bool
    )


def _dspy():
    """Return the dspy module, importing it on first call (memoised).

    Mirrors ``clio_agent.config._dspy``: dspy is imported lazily because a
    top-level ``import dspy`` costs several seconds on some frameworks Pythons
    and this module is on hot boot paths.
    """
    global _dspy_cache  # noqa: PLW0603
    if _dspy_cache is None:
        import dspy  # noqa: PLC0415

        _dspy_cache = dspy
    return _dspy_cache


def _construct_lm(*, model: str, **lm_kwargs: Any) -> dspy.LM:
    """Construct a dspy.LM that emits an ``lm.call`` trace event per call.

    Always uses the trace-emitting subclass so each call folds into the canonical
    trace when a GACT turn is active; a cheap no-op otherwise (CLI/optimizer).
    """
    _dspy()  # ensure dspy is importable/configured before constructing the LM
    endpoint = urlparse(str(lm_kwargs.get("api_base") or ""))
    # No URL userinfo, query, API keys or prompt text in construction diagnostics.
    logger.info("lm constructed model=%s endpoint_host=%s", model, endpoint.hostname or "default")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "lm construction callers=%s",
            " <- ".join(frame.name for frame in traceback.extract_stack(limit=8)[:-1]),
        )
    _warn_dropped_params(model=model, kwargs=lm_kwargs)
    return _io_logging_lm_cls()(model=model, **lm_kwargs)


def create_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a dspy.LM instance from provider config.

    For openai/anthropic, uses the provider prefix (e.g., 'openai/gpt-4o-mini').
    For lm_studio, uses 'openai/{model}' with custom api_base. For ollama, uses
    LiteLLM's native 'ollama_chat/{model}' (see :func:`_connection_kwargs` for
    why its api_base must NOT carry a trailing ``/v1``).
    For codex/claude_code, uses a provider-specific prefix routed through
    the LiteLLM ``CustomLLM`` registered by ``providers.*_litellm``.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance
    """
    # Defer litellm's eager ~40 MB cl100k_base tiktoken load until first real
    # encode (see lm.lazy_tiktoken). MUST run before the provider import below,
    # which is the first ``import litellm`` in the server. Config-gated so an
    # operator can opt out; default on. Never raises.
    if _defer_tiktoken_enabled():
        from clio_agent.lm.lazy_tiktoken import install_lazy_cl100k  # noqa: PLC0415

        install_lazy_cl100k()
    else:
        # Opted out of the lazy proxy: litellm's import loads cl100k eagerly, so the
        # vendored rank files still need repairing first (see lm.tiktoken_vendored).
        from clio_agent.lm.tiktoken_vendored import repair_vendored_rank_files  # noqa: PLC0415

        repair_vendored_rank_files()
    _ensure_provider_registered(config)
    if _defer_tiktoken_enabled():
        # After litellm is imported (by the provider registration above): stop its
        # response-cost recount from re-materialising the ~40 MB cl100k vocab on the
        # first turn. clio does not consume litellm's response_cost (#930).
        from clio_agent.lm.lazy_tiktoken import disable_litellm_cost_recount  # noqa: PLC0415

        disable_litellm_cost_recount()
    _resolve_lm_studio_model_if_needed(config)
    model_name = _resolve_model_name(config)

    extras = build_request_kwargs(config, role="main")
    connection = _connection_kwargs(config)
    lm = _construct_lm(
        model=model_name,
        api_key=config.api_key,
        max_tokens=config.max_tokens or None,
        model_type="chat",
        # iowarp/clio-agent#8: disable DSPy LM cache so token usage
        # always lands in dspy.settings.usage_tracker (cache_hits
        # short-circuit before add_usage fires). Real serving means
        # identical prompts should still bill — accounting matters
        # more than the small spend saved on duplicate questions.
        cache=False,
        **connection,
        **extras,
    )
    # Keep the catalog identity on the LM so the generic stream tap can label
    # provider-native reasoning without inferring identity from a LiteLLM prefix.
    # Codex and Claude Code own their established stream semantics and are
    # explicitly excluded from the generic provider bridge.
    provider_id = config.provider_id or str(config.provider)
    try:
        lm._clio_provider_id = provider_id  # type: ignore[attr-defined]
        lm._clio_provider_config = replace(  # type: ignore[attr-defined]
            config, provider_options=dict(config.provider_options)
        )
        lm._clio_reasoning_fallback = provider_id not in {  # type: ignore[attr-defined]
            "codex",
            "claude_code",
        }
    except Exception:  # noqa: BLE001,S110 - never let tagging break LM construction
        pass
    # A per-forward LM reaches the running turn's usage rollup only via this.
    turn_lm_ledger.record(lm)
    return lm


def _ensure_provider_registered(config: LMProviderConfig) -> None:
    """Register provider-specific LiteLLM hooks before constructing dspy.LM.

    Only CLI-backed providers need this today (they are LiteLLM CustomLLMs).
    The import is gated on the provider so installs without the relevant
    binary do not pay the import cost.
    """
    if config.provider == "codex":
        if config.codex_variant == "sdk":
            from clio_agent.providers.codex.sdk_transport import (  # noqa: PLC0415
                ensure_registered,
            )
        else:
            from clio_agent.providers.codex.litellm_adapter import (  # noqa: PLC0415
                ensure_registered,
            )

        ensure_registered()
    elif config.provider == "claude_code":
        from clio_agent.providers.claude_code_litellm import (  # noqa: PLC0415
            ensure_registered,
        )

        ensure_registered()


def _resolved_litellm_prefix(config: LMProviderConfig) -> str:
    """The LiteLLM provider prefix for ``config``'s catalog preset.

    Falls back to ``config.provider`` (the wire kind) when no preset row
    matches. codex/claude_code never reach this — :func:`_resolve_model_name`
    prefixes those itself — so this only serves the OpenAI-compatible and
    native (``ollama_chat``) dialects. Shared by :func:`_resolve_model_name`
    and :func:`_connection_kwargs` so the catalog lookup isn't duplicated.
    """
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    preset = get_provider(getattr(config, "provider_id", "") or config.provider)
    return preset.litellm_prefix if preset is not None else config.provider


def _connection_kwargs(config: LMProviderConfig) -> dict[str, str]:
    """Build the ``api_base`` kwarg LiteLLM needs to reach ``config``'s endpoint.

    LiteLLM's native ``ollama_chat`` provider appends its own ``/api/chat`` to
    whatever ``api_base`` it is given (``OllamaChatConfig.get_complete_url``).
    An ``api_base`` that still carries the OpenAI-compatible ``/v1`` suffix —
    the shape every other dialect here uses — doubles into ``/v1/api/chat``
    and 404s (iowarp/clio-agent#1413). Routing through the shared
    :func:`~clio_agent.providers.api_base.native_root` helper here repairs a
    config a user already saved with a ``/v1`` base, not just new ones.
    """
    if not config.api_base:
        return {}
    api_base = config.api_base
    if _resolved_litellm_prefix(config) == "ollama_chat":
        from clio_agent.providers.api_base import native_root  # noqa: PLC0415

        api_base = native_root(api_base)
    return {"api_base": api_base}


def _resolve_model_name(config: LMProviderConfig) -> str:
    """Prefix the configured model id for litellm.

    - ``openai`` / ``anthropic``: native litellm prefix.
    - ``codex`` / ``claude_code``: route through registered CustomLLMs
      under provider-specific prefixes. ``codex``'s litellm-facing prefix is
      deliberately NOT "codex" -- see
      :data:`clio_agent.providers.codex.constants.LITELLM_PROVIDER`.
    - ``ollama``: LiteLLM's native ``ollama_chat/`` provider (see
      :func:`_connection_kwargs` for its api_base requirement).
    - everything else (lm_studio, argonne, vllm, …): treated as
      OpenAI-compatible by litellm, so we prefix with ``openai/``.

    Generic OpenAI-compatible endpoints receive one LiteLLM ``openai/``
    provider prefix. Argonne's Sophia gateway is a special case: some
    served model ids themselves start with ``openai/`` (for example
    ``openai/gpt-oss-120b``), and LiteLLM strips the first segment as
    the provider name. Sending ``openai/openai/gpt-oss-120b`` is how we
    preserve the actual Sophia model id on the wire. Metis does not need
    that double prefix.
    """
    if not config.model.strip():
        raise ValueError(
            f"No model configured for LM provider {config.provider_id or config.provider!r}"
        )
    if config.provider == "codex":
        # Strip a legacy/already-litellm-prefixed value defensively (a
        # persisted config.model could in principle already carry either
        # transport's prefix) before re-applying the CURRENT litellm-facing
        # prefix for the BOUND transport (S1b: sdk vs direct).
        bare = (
            config.model.removeprefix(f"{_CODEX_LITELLM_PREFIX}/")
            .removeprefix(f"{_CODEX_LITELLM_PREFIX_SDK}/")
            .removeprefix("codex/")
            .removeprefix("cg-")
        )
        prefix = (
            _CODEX_LITELLM_PREFIX_SDK if config.codex_variant == "sdk" else _CODEX_LITELLM_PREFIX
        )
        return f"{prefix}/cg-{bare}"
    if config.provider == "claude_code":
        bare = config.model.removeprefix("claude_code/").removeprefix("cc-")
        return f"claude_code/cc-{bare}"
    prefix = _resolved_litellm_prefix(config)
    bare = config.model.removeprefix(f"{prefix}/")
    return f"{prefix}/{bare}"


def _is_argonne_sophia(config: LMProviderConfig) -> bool:
    """Retain the historical helper for callers that inspect ALCF endpoints."""

    parsed = urlparse(config.api_base)
    return config.provider == "argonne" and "/resource_server/sophia/" in parsed.path


def create_planner_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a lower-temperature LM for deterministic action planning.

    Uses config.planner_temperature instead of config.temperature (item 1:
    "planner and router determinism may set temperature explicitly, but only
    when temperature is in the effective parameter set" —
    :func:`~clio_agent.lm.request_builder.build_request_kwargs`'s
    ``role="planner"`` still gates it).

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance with lower planner temperature
    """
    _ensure_provider_registered(config)
    _resolve_lm_studio_model_if_needed(config)
    model_name = _resolve_model_name(config)

    connection = _connection_kwargs(config)
    lm = _construct_lm(
        model=model_name,
        api_key=config.api_key,
        max_tokens=config.planner_max_tokens or None,
        model_type="chat",
        cache=False,  # see create_lm — same rationale
        **connection,
        **build_request_kwargs(config, role="planner"),
    )
    # Stamp the effective planner sampling surface. Secondary inference from a
    # planner call must not silently revert to the main LM's temperature/cap.
    lm._clio_provider_config = replace(  # type: ignore[attr-defined]
        config,
        temperature=config.planner_temperature,
        max_tokens=config.planner_max_tokens or 0,
        provider_options=dict(config.provider_options),
    )
    return lm


def _resolve_lm_studio_model_if_needed(config: LMProviderConfig) -> None:
    """Fill a blank LM Studio model from the currently loaded model list."""
    # Resolve discovery through ``clio_agent.config`` (not the owning module) so the
    # ``clio_agent.config.list_lm_studio_models`` monkeypatch seam tests rely on is
    # honoured (config re-exports these from providers.lmstudio_discovery).
    from clio_agent.config import (  # noqa: PLC0415
        list_lm_studio_models,
        select_models_for_agents,
    )

    if config.provider == "lm_studio" and not config.model.strip():
        models = list_lm_studio_models(base_url=config.api_base)
        config.model, _ = select_models_for_agents(models)


#: Top-level OpenAI-shaped kwargs clio ever passes that a LiteLLM dialect
#: validates against `get_supported_openai_params` before honoring. Nested
#: `extra_body` contents (top_k, min_p, chat_template_kwargs, ...) are forwarded
#: raw by OpenAI-compatible dialects and are never subject to that check, so
#: they are intentionally not in this list.
_CHECKED_PARAM_NAMES: tuple[str, ...] = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "reasoning_effort",
    "thinking",
    "max_tokens",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "n",
    "seed",
)

#: LiteLLM ``CustomLLM`` transports clio owns end-to-end
#: (`providers.codex.litellm_adapter`, `providers.codex.sdk_transport`,
#: `providers.claude_code_litellm`). LiteLLM's provider registry does not know
#: these as dialects -- `get_llm_provider`/`get_supported_openai_params` raise
#: or return nonsense for them -- and their own `completion()` reads a small,
#: fixed set of `optional_params` keys directly, ignoring everything else. The
#: drop_params proactive check below does not apply to them.
_CUSTOM_TRANSPORT_PREFIXES: tuple[str, ...] = (
    f"{_CODEX_LITELLM_PREFIX}/",
    f"{_CODEX_LITELLM_PREFIX_SDK}/",
    "claude_code/",
)


def _warn_dropped_params(*, model: str, kwargs: dict[str, Any]) -> None:
    """Log, at WARNING, every optional kwarg LiteLLM's ``drop_params=True`` would drop.

    LiteLLM exposes no reliable after-the-fact hook for an actual drop: its
    ``drop_params`` machinery (``litellm/utils.py`` -- ``_get_non_default_params``
    and the per-call-type ``get_optional_params*`` functions) just pops the
    unsupported key with no callback or log line (verified against litellm
    1.102.1's source). So this checks PROACTIVELY, at LM construction: for each
    optional kwarg clio is about to pass, is it in this model/dialect's own
    ``get_supported_openai_params()`` list? Anything not listed there WOULD be
    silently dropped on the real call. A drop means one of clio's own
    capability records is wrong for this endpoint/model (model-capabilities
    plan, Part 2.4) -- these are bug signals, not expected noise.

    Best-effort and purely diagnostic: any failure here (unmapped dialect,
    litellm quirk) is logged at DEBUG and never raises -- this must never break
    LM construction.
    """
    if model.startswith(_CUSTOM_TRANSPORT_PREFIXES):
        return
    present = [name for name in _CHECKED_PARAM_NAMES if name in kwargs]
    if not present:
        return
    try:
        import litellm  # noqa: PLC0415

        bare_model, custom_llm_provider, _key, _base = litellm.get_llm_provider(
            model, api_base=kwargs.get("api_base") or None
        )
        supported = set(
            litellm.get_supported_openai_params(
                model=bare_model, custom_llm_provider=custom_llm_provider
            )
            or []
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break LM construction
        logger.debug("drop_params check skipped for model=%s: %s", model, exc)
        return
    dropped = [name for name in present if name not in supported]
    if dropped:
        endpoint = urlparse(str(kwargs.get("api_base") or ""))
        logger.warning(
            "lm drop_params would silently drop params=%s model=%s endpoint_host=%s "
            "-- one of clio's capability records is wrong for this endpoint/model",
            dropped,
            model,
            endpoint.hostname or "default",
        )
