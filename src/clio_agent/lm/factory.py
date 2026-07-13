"""LM construction/factory layer.

Extracted from :mod:`clio_agent.config` (#769). ``clio_agent.config`` re-exports
every public name here so historical import seams (and their monkeypatch points)
keep working; new code should import from :mod:`clio_agent.lm.factory` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    import dspy

    from clio_agent.config import LMProviderConfig

from clio_agent.lm.adapters import _reasoning_model_capability
from clio_agent.lm.io_logging import _io_logging_lm_cls

_dspy_cache = None


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
    return _io_logging_lm_cls()(model=model, **lm_kwargs)


def create_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a dspy.LM instance from provider config.

    For openai/anthropic, uses the provider prefix (e.g., 'openai/gpt-4o-mini').
    For lm_studio/ollama, uses 'openai/{model}' with custom api_base.
    For codex/claude_code, uses a provider-specific prefix routed through
    the LiteLLM ``CustomLLM`` registered by ``providers.*_litellm``.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance
    """
    _ensure_provider_registered(config)
    _resolve_lm_studio_model_if_needed(config)
    model_name = _resolve_model_name(config)

    extras = _provider_lm_kwargs(config)
    lm = _construct_lm(
        model=model_name,
        api_base=config.api_base,
        api_key=config.api_key,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        model_type="chat",
        # iowarp/clio-agent#8: disable DSPy LM cache so token usage
        # always lands in dspy.settings.usage_tracker (cache_hits
        # short-circuit before add_usage fires). Real serving means
        # identical prompts should still bill — accounting matters
        # more than the small spend saved on duplicate questions.
        cache=False,
        **extras,
    )
    # Per-model gate for the content<-reasoning_content extraction in
    # IOLoggingLM._process_completion (reasoning models only; today qwopus/qwen).
    try:
        lm._clio_reasoning_fallback = _reasoning_model_capability(config)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001,S110 - never let tagging break LM construction
        pass
    return lm


def _ensure_provider_registered(config: LMProviderConfig) -> None:
    """Register provider-specific LiteLLM hooks before constructing dspy.LM.

    Only CLI-backed providers need this today (they are LiteLLM CustomLLMs).
    The import is gated on the provider so installs without the relevant
    binary do not pay the import cost.
    """
    if config.provider == "codex":
        from clio_agent.providers.codex_litellm import ensure_registered  # noqa: PLC0415

        ensure_registered()
    elif config.provider == "claude_code":
        from clio_agent.providers.claude_code_litellm import (  # noqa: PLC0415
            ensure_registered,
        )

        ensure_registered()


def _resolve_model_name(config: LMProviderConfig) -> str:
    """Prefix the configured model id for litellm.

    - ``openai`` / ``anthropic``: native litellm prefix.
    - ``codex`` / ``claude_code``: route through registered CustomLLMs
      under provider-specific prefixes.
    - everything else (lm_studio, ollama, argonne, …): treated as
      OpenAI-compatible by litellm, so we prefix with ``openai/``.

    Generic OpenAI-compatible endpoints receive one LiteLLM ``openai/``
    provider prefix. Argonne's Sophia gateway is a special case: some
    served model ids themselves start with ``openai/`` (for example
    ``openai/gpt-oss-120b``), and LiteLLM strips the first segment as
    the provider name. Sending ``openai/openai/gpt-oss-120b`` is how we
    preserve the actual Sophia model id on the wire. Metis does not need
    that double prefix.
    """
    if config.provider in ("openai", "anthropic"):
        return f"{config.provider}/{config.model}"
    if config.provider == "codex":
        bare = config.model.removeprefix("codex/").removeprefix("cdx-")
        return f"codex/cdx-{bare}"
    if config.provider == "claude_code":
        bare = config.model.removeprefix("claude_code/").removeprefix("cc-")
        return f"claude_code/cc-{bare}"
    bare = config.model
    if _is_argonne_sophia(config) and bare.startswith("openai/"):
        return f"openai/{bare}"
    if bare.startswith("openai/"):
        bare = bare[len("openai/") :]
    return f"openai/{bare}"


def _is_argonne_sophia(config: LMProviderConfig) -> bool:
    """Return whether the Argonne config targets Sophia's vLLM gateway."""
    parsed = urlparse(config.api_base)
    return config.provider == "argonne" and "/resource_server/sophia/" in parsed.path


def _thinking_kwargs(config: LMProviderConfig) -> dict:
    """Translate thinking_budget to provider-specific litellm kwargs.

    Anthropic: extended-thinking is configured via the ``thinking``
    parameter (budget_tokens controls how much reasoning the model
    spends). DSPy/litellm pass it through as a kwarg.

    OpenAI / openai-compatible: rough mapping of token budget to
    `reasoning_effort` ('low' | 'medium' | 'high'). Most openai-compat
    proxies ignore unknown kwargs gracefully.

    All other providers: returns {} so we don't trip on unsupported
    parameters.
    """
    n = int(getattr(config, "thinking_budget", 0) or 0)
    if n <= 0:
        return {}
    if config.provider == "anthropic":
        return {"thinking": {"type": "enabled", "budget_tokens": n}}
    if config.provider in (
        "openai",
        "lm_studio",
        "ollama",
        "argonne",
        "codex",
        "claude_code",
    ):
        if n < 2000:
            effort = "low"
        elif n < 8000:
            effort = "medium"
        else:
            effort = "high"
        return {"reasoning_effort": effort}
    return {}


def create_planner_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a lower-temperature LM for deterministic action planning.

    Uses config.planner_temperature instead of config.temperature.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance with lower planner temperature
    """
    _ensure_provider_registered(config)
    _resolve_lm_studio_model_if_needed(config)
    model_name = _resolve_model_name(config)

    return _construct_lm(
        model=model_name,
        api_base=config.api_base,
        api_key=config.api_key,
        temperature=config.planner_temperature,
        max_tokens=config.planner_max_tokens,
        model_type="chat",
        cache=False,  # see create_lm — same rationale
        **_provider_lm_kwargs(config),
    )


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


def _thinking_disabled() -> bool:
    """Whether reasoning ("thinking") is disabled for the active LM.

    Resolved via ``lm.disable_thinking`` / ``CLIO_LM_DISABLE_THINKING``
    (file → env → default False). Shared by the sampling-kwargs path
    (``_provider_lm_kwargs``) and the output-discipline prompt injection in
    ``gact.agents.builders`` so both honour a single knob and one truthy rule.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    return bool(
        conf.resolve(
            "lm.disable_thinking",
            env="CLIO_LM_DISABLE_THINKING",
            default=False,
            cast=conf.as_bool,
        )
    )


def _provider_lm_kwargs(config: LMProviderConfig) -> dict[str, Any]:
    """Return provider-specific LiteLLM kwargs for dspy.LM construction."""
    extras = _thinking_kwargs(config)
    # Qwen-family reasoning models (e.g. qwopus) run their reasoning_content away
    # on the pipeline's structured routing/tool-decision calls — consuming the whole
    # token budget without reaching the decision (uncapped → >900s → wedge; capped →
    # no tool call). Structured routing does not need chain-of-thought, so disable
    # thinking when CLIO_LM_DISABLE_THINKING is set. enable_thinking=false is honored
    # by Qwen chat templates (verified: 8327 reasoning chars/43s → 0 chars/0.8s).
    if _thinking_disabled():
        body = dict(extras.get("extra_body") or {})
        body["chat_template_kwargs"] = {
            **body.get("chat_template_kwargs", {}),
            "enable_thinking": False,
        }
        extras["extra_body"] = body
    # Sampling surface. top_p / presence_penalty are OpenAI-standard (litellm
    # forwards them directly); top_k / min_p are non-OpenAI, forwarded to the
    # backend (llama.cpp / LM Studio / vLLM) via extra_body. None -> omit (use the
    # model's own default).
    if config.top_p is not None:
        extras["top_p"] = config.top_p
    if config.presence_penalty is not None:
        extras["presence_penalty"] = config.presence_penalty
    if config.top_k is not None or config.min_p is not None:
        body = dict(extras.get("extra_body") or {})
        if config.top_k is not None:
            body["top_k"] = config.top_k
        if config.min_p is not None:
            body["min_p"] = config.min_p
        extras["extra_body"] = body
    # Reasoning-model trajectory-regurgitation stop sequences (per-model). On a long
    # trajectory, qwopus continues/fabricates DSPy's trajectory INPUT format —
    # underscore-numbered `thought_N`/`tool_name_N`/`tool_args_N` + invented
    # `observation_N` tool results — instead of emitting one step (react) or the
    # answer (extract), running away to truncation -> unparseable. The model must
    # NEVER emit those markers (its real outputs are next_thought/next_tool_name/
    # next_tool_args/reasoning/answer, with NO underscore-number), so they are safe
    # stop sequences: generation halts the instant regurgitation starts and the
    # valid leading fields survive. Override with CLIO_LM_STOP_SEQUENCES (||-joined).
    if _reasoning_model_capability(config) and "stop" not in extras:
        from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

        # File layer accepts a YAML list; the env override stays ``||``-joined (a
        # comma is a legal stop token, so csv-splitting would be wrong here).
        raw_stop = conf.resolve("lm.stop_sequences", env="CLIO_LM_STOP_SEQUENCES", default=None)
        override_stop: list[str]
        if isinstance(raw_stop, (list, tuple)):
            override_stop = [str(s) for s in raw_stop if str(s)]
        elif raw_stop:
            override_stop = [s for s in str(raw_stop).split("||") if s]
        else:
            override_stop = []
        extras["stop"] = (
            override_stop
            if override_stop
            else [
                "[[ ## observation",
                "[[ ## thought_",
                "[[ ## tool_name_",
                "[[ ## tool_args_",
            ]
        )
    if config.provider == "codex":
        extras["codex_transport"] = config.codex_transport
    elif config.provider == "claude_code":
        extras["claude_code_transport"] = config.claude_code_transport
    return extras
