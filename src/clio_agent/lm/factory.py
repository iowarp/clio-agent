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

from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.codex.constants import LITELLM_PROVIDER as _CODEX_LITELLM_PREFIX
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

    The LM carries the ``lm.call`` callback, so each call folds into the canonical
    trace when a GACT turn is active (an audit row only otherwise: CLI/optimizer).
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
    from clio_agent.lm.call_trace import LM_CALL_TRACE  # noqa: PLC0415 - dspy stays lazy
    from clio_agent.lm.policy import lm_retries  # noqa: PLC0415

    dspy = _dspy()
    lm_kwargs.setdefault("num_retries", lm_retries())
    return dspy.LM(model=model, callbacks=[LM_CALL_TRACE], **lm_kwargs)


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
    if config.provider == "codex":
        return _record_identity(_codex_direct_lm(config), config)
    if config.provider == "claude_code":
        return _record_identity(_claude_code_lm(config), config)
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
    if _defer_tiktoken_enabled():
        # After litellm is imported (by the provider registration above): stop its
        # response-cost recount from re-materialising the ~40 MB cl100k vocab on the
        # first turn. clio does not consume litellm's response_cost (#930).
        from clio_agent.lm.lazy_tiktoken import disable_litellm_cost_recount  # noqa: PLC0415

        disable_litellm_cost_recount()
    _resolve_lm_studio_model_if_needed(config)
    model_name = _resolve_model_name(config)

    extras = build_request_kwargs(config)
    connection = _connection_kwargs(config)
    lm = _construct_lm(
        model=model_name,
        # A keyless local server has no credential: an empty key is omitted, never sent.
        **({"api_key": config.api_key} if config.api_key else {}),
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
    # Only the Anthropic Messages API takes images/documents inside a tool result;
    # every chat-completions server takes text-only tool rows (lm15 refuses media there).
    lm._clio_tool_result_media = (
        "native" if _resolved_litellm_prefix(config) == "anthropic" else "user_message"
    )
    return _record_identity(lm, config)


def _record_identity(lm: Any, config: LMProviderConfig) -> Any:
    """Tag the LM with its catalog identity and join the running turn's usage ledger."""
    # Keep the catalog identity on the LM so the generic stream tap can label
    # provider-native reasoning without inferring identity from a LiteLLM prefix.
    # Codex and Claude Code own their established stream semantics and are
    # explicitly excluded from the generic provider bridge.
    provider_id = config.provider_id or str(config.provider)
    try:
        lm._clio_provider_id = provider_id
        lm._clio_provider_config = replace(config, provider_options=dict(config.provider_options))
        lm._clio_reasoning_fallback = provider_id not in {"codex", "claude_code"}
    except Exception:  # noqa: BLE001,S110 - never let tagging break LM construction
        pass
    # A per-forward LM reaches the running turn's usage rollup only via this.
    turn_lm_ledger.record(lm)
    return lm


def _claude_code_bare_model(config: LMProviderConfig) -> str:
    """The Claude Code model id without any transport prefix a persisted value carries."""
    return config.model.removeprefix("claude_code/").removeprefix("cc-")


def _claude_code_lm(config: LMProviderConfig) -> Any:
    """Claude Code: a ``dspy.LM`` on the Claude Code SDK engine pair.

    The thinking config is the engine's (fixed per LM, part of the session key); the
    remaining generation kwargs stay on the LM for callers that build their
    ``Request.config`` from them.
    """
    import dspy  # noqa: PLC0415

    from clio_agent.lm.call_trace import LM_CALL_TRACE  # noqa: PLC0415
    from clio_agent.lm.policy import lm_retries  # noqa: PLC0415
    from clio_agent.providers.claude_code_engine import (  # noqa: PLC0415
        AsyncClaudeCodeEngine,
        ClaudeCodeEngine,
    )

    if not config.model.strip():
        raise ValueError("No model configured for LM provider 'claude_code'")
    bare = _claude_code_bare_model(config)
    extras = build_request_kwargs(config)
    thinking = extras.pop("claude_code_thinking", None)
    generation = {k: v for k, v in extras.items() if not k.startswith("claude_code_")}
    if config.max_tokens:
        generation["max_tokens"] = config.max_tokens
    lm = dspy.LM(
        f"claude_code/{bare}",
        engine=ClaudeCodeEngine(bare, thinking=thinking),
        async_engine=AsyncClaudeCodeEngine(bare, thinking=thinking),
        cache=False,
        num_retries=lm_retries(),
        model_type="chat",
        callbacks=[LM_CALL_TRACE],
        **generation,
    )
    lm._clio_tool_result_media = "native"  # the engine sends tool-result media natively
    return lm


def _codex_direct_lm(config: LMProviderConfig) -> Any:
    """Codex direct: a ``dspy.LM`` on the Codex direct engine pair (lm15 wire, kept WS).

    The reasoning effort rides the request config (``reasoning_effort``); the
    per-conversation ``prompt_cache_key`` is declared so the loop sends it.
    """
    import dspy  # noqa: PLC0415

    from clio_agent.lm.call_trace import LM_CALL_TRACE  # noqa: PLC0415
    from clio_agent.lm.policy import lm_retries  # noqa: PLC0415
    from clio_agent.providers.codex.direct_engine import (  # noqa: PLC0415
        AsyncCodexDirectEngine,
        CodexDirectEngine,
        default_wire,
    )

    if not config.model.strip():
        raise ValueError("No model configured for LM provider 'codex'")
    bare = _codex_bare_model(config)
    extras = build_request_kwargs(config)
    effort = extras.pop("codex_reasoning_effort", None)
    generation = {k: v for k, v in extras.items() if not k.startswith("codex_")}
    if effort is not None:
        generation["reasoning_effort"] = effort
    if config.max_tokens:
        generation["max_tokens"] = config.max_tokens
    wire = default_wire()
    http = config.codex_transport == "sse"  # the operator's explicit no-WebSocket choice
    lm = dspy.LM(
        f"{_CODEX_LITELLM_PREFIX}/{bare}",
        engine=CodexDirectEngine(bare, wire=wire, http=http),
        async_engine=AsyncCodexDirectEngine(bare, wire=wire, http=http),
        cache=False,
        num_retries=lm_retries(),
        model_type="chat",
        callbacks=[LM_CALL_TRACE],
        **generation,
    )
    lm._clio_prompt_cache_key = True
    lm._clio_tool_result_media = "native"  # Responses tool outputs carry images and files
    return lm


def _codex_bare_model(config: LMProviderConfig) -> str:
    """The Codex model id without any transport prefix a persisted value may carry."""
    return (
        config.model.removeprefix(f"{_CODEX_LITELLM_PREFIX}/")
        .removeprefix("codex/")
        .removeprefix("cg-")
    )


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

    ``config.model`` is always the provider's OWN model id (discovery takes
    it verbatim from the endpoint's ``/models``), and LiteLLM strips exactly
    one leading segment as its provider name. So the catalog prefix is
    always prepended and the id is never trimmed: a provider id that itself
    begins with the prefix (OpenRouter's ``openrouter/free`` router, a served
    ``openai/gpt-oss-120b``) must reach the wire intact. Trimming it sent
    ``"model": "free"`` to OpenRouter, which 404s every turn.
    """
    if not config.model.strip():
        raise ValueError(
            f"No model configured for LM provider {config.provider_id or config.provider!r}"
        )
    if config.provider == "codex":
        # Strip an already-litellm-prefixed value (a persisted config.model may
        # carry the direct prefix) before re-applying it.
        return f"{_CODEX_LITELLM_PREFIX}/{_codex_bare_model(config)}"  # the engine LM's model
    if config.provider == "claude_code":
        return f"claude_code/{_claude_code_bare_model(config)}"  # the engine LM's model
    return f"{_resolved_litellm_prefix(config)}/{config.model}"


def _is_argonne_sophia(config: LMProviderConfig) -> bool:
    """Retain the historical helper for callers that inspect ALCF endpoints."""

    parsed = urlparse(config.api_base)
    return config.provider == "argonne" and "/resource_server/sophia/" in parsed.path


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
