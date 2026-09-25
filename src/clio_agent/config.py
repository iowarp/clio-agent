#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy>=3.1.3",
#   "fastmcp>=3.2.4",
#   "requests>=2.31.0",
# ]
# ///

"""
ClioAgent Configuration Module

Multi-provider LM configuration with environment-based settings.
Supports local, cloud, OpenAI-compatible, Codex, and ALCF providers.

Usage:
    >>> from clio_agent.config import setup_dspy
    >>> lm = setup_dspy()

    >>> # Or with environment-based config
    >>> from clio_agent.config import load_config_from_env, create_lm
    >>> config = load_config_from_env()
    >>> lm = create_lm(config)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, cast
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# dspy lives behind a lazy import — top-level ``import dspy`` costs
# ~4 s on Aurora's frameworks Python (litellm + transitive deps), and
# every ``runtime.status`` / ``gact.app`` boot path imports config.py
# transitively. Functions that actually need dspy import it inside
# their body via ``_dspy()`` below; type-only references stay valid
# thanks to ``from __future__ import annotations`` (PEP 563).
if TYPE_CHECKING:  # pragma: no cover
    # Annotations only -- `from __future__ import annotations` keeps
    # the runtime import elided. Needed in scope so `dspy.LM` return
    # types lint cleanly under F821.
    import dspy


_dspy_cache = None


def _dspy():
    """Return the dspy module, importing it on first call.

    Memoised in the module so subsequent calls are free. All callers
    inside this module funnel through here so we don't accidentally
    re-add a top-level ``import dspy`` later.
    """
    global _dspy_cache  # noqa: PLW0603
    if _dspy_cache is None:
        import dspy  # noqa: PLC0415

        _dspy_cache = dspy
    return _dspy_cache


# ============================================================================
# PROVIDER DEFAULTS — derived from clio_agent.providers.catalog
# ============================================================================

#
# These two dicts are derived views over the canonical provider list at
# ``src/clio_agent/providers/catalog.py``. Add a new provider by adding
# a ``Provider(...)`` entry there — the wire defaults flow through here,
# the catalog rows flow into ``GET /v1/providers/lm``, and the static
# model fallback flows into ``GET /v1/providers/{id}/models``.
#
# Per-provider capability flags currently tracked:
#   strip_openai_prefix : strip leading "openai/"/"anthropic/" from the
#                         configured model id before sending. Defaults
#                         to True for most generic openai-compat proxies.
#                         False for backends that use HuggingFace-style
#                         ids verbatim (Argonne / ALCF — `openai/gpt-oss
#                         -120b` IS the gateway's model id; stripping
#                         turns it into `gpt-oss-120b`, which the
#                         gateway maps to a non-existent backend).
#   (the static per-provider `max_tokens_default`/`supports_vision` overrides
#    that used to live here were deleted -- model-capabilities brief 9.1;
#    `resolve_effective_max_tokens` prefers the handshake-discovered output
#    limit, and vision comes from the effective capabilities, Part 5.5)
from clio_agent.providers import credentials as _credentials
from clio_agent.providers.catalog import (
    as_cloud_api_key_env as _registry_cloud_api_key_env,
)
from clio_agent.providers.catalog import (
    as_provider_defaults_dict as _registry_provider_defaults,
)
from clio_agent.providers.catalog import get_provider as _catalog_provider
from clio_agent.providers.catalog import kind_default as _catalog_kind_default
from clio_agent.providers.catalog import normalize_provider_options as _normalize_provider_options
from clio_agent.providers.catalog import provider_defaults as _catalog_provider_defaults
from clio_agent.providers.catalog_types import ProviderKind

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = _registry_provider_defaults()

# Environment variable names for cloud provider API keys.
_CLOUD_API_KEY_ENV: dict[str, str] = _registry_cloud_api_key_env()

ENV_FILE_LOADED_KEY = "CLIO_ENV_FILE_LOADED"


def load_project_env_file(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
) -> Path | None:
    """Load CLIO environment defaults from a dotenv-style file.

    This keeps host/model defaults outside Python code while making direct
    commands like ``uv run src/clio_agent/ui/cli.py`` pick up the repo-local
    configuration. Existing process environment variables win unless
    ``override`` is true.
    """
    env_file = _resolve_env_file(path)
    if env_file is None or not env_file.exists():
        return None

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value

    os.environ[ENV_FILE_LOADED_KEY] = str(env_file)
    return env_file


def _resolve_env_file(path: str | os.PathLike[str] | None) -> Path | None:
    explicit = path or os.environ.get("CLIO_ENV_FILE", "")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env.resolve(strict=False)

    repo_env = Path(__file__).resolve().parents[2] / ".env"
    if repo_env.exists():
        return repo_env.resolve(strict=False)
    return None


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").strip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


# ============================================================================
# MULTI-PROVIDER CONFIGURATION
# ============================================================================


@dataclass
class LMProviderConfig:
    """Multi-provider LM configuration.

    Supports lm_studio, ollama, openai, and anthropic providers.
    Defaults are loaded from PROVIDER_DEFAULTS based on provider name.

    Attributes:
        provider: LM provider name
        api_base: API base URL
        model: Model identifier
        api_key: API key
        temperature: Sampling temperature
        max_tokens: Maximum tokens per response
        planner_temperature: Lower temperature for deterministic action planning
        planner_max_tokens: Maximum tokens for planner JSON generation
        environment: Deployment environment (dev/staging/production)
        codex_transport: Codex transport: "sdk" (the only supported transport)
    """

    # ProviderKind (the dialect selector, not an identity -- Part 3): the
    # catalog owns the one list of kinds, so it isn't re-typed here too.
    provider: ProviderKind = "lm_studio"
    provider_id: str = ""
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    provider_options: dict[str, str] = field(default_factory=dict)
    # None omits the field entirely (model-capabilities brief Part 7 item 1):
    # with no value, the server applies the model maker's own default (vLLM's
    # generation_config, Ollama's Modelfile) instead of clio silently forcing
    # temp-0 on every model, which degenerates Qwen-family reasoning models
    # (qwopus, nemotron) into endless verbatim repetition loops. Explicit
    # modules and callers may still set it; `lm.request_builder` sends it only
    # when the effective parameter set (Part 5.5) actually accepts it.
    temperature: float | None = None
    # 0 omits the client output cap; positive values set an explicit cap.
    max_tokens: int = 0
    planner_temperature: float = 0.3
    planner_max_tokens: int | None = None
    router_temperature: float | None = None
    # Sampling surface (None = omit -> the provider/model's own default applies).
    # Greedy decoding (temperature 0) makes Qwen-family REASONING models (qwopus,
    # nemotron) degenerate into endless verbatim repetition loops -- Qwen's own docs
    # say DO NOT use greedy decoding and recommend temp 0.6 / top_p 0.95 / top_k 20
    # for thinking mode. These expose that full sampling surface so a reasoning model
    # can be driven at its recommended settings instead of the temp-0 default (which
    # only suits short non-reasoning structured routing). top_p/presence_penalty are
    # OpenAI-standard; top_k/min_p are forwarded via extra_body (llama.cpp/LM Studio).
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    environment: str = "dev"
    codex_transport: Literal["sdk"] = "sdk"
    # "sdk" (the only transport since v0.8.0): the in-process Claude Agent SDK
    #   with a persistent CLI session — no per-call spawn, streaming-capable, and
    #   setting_sources=[] keeps the user's ~/.claude/CLAUDE.md out of the prompt.
    #   Needs the claude-agent-sdk package (the `claude-code` extra). The legacy
    #   "exec" batch transport (one `claude -p` per call, ~10-15s cold start,
    #   #715) was deleted in the v0.8.0 cleanup.
    claude_code_transport: Literal["sdk"] = "sdk"
    # Reasoning/thinking budget (explicit token override). Mapped per-provider
    # in create_lm via lm.dialect_wire.thinking_wire (model's own ThinkingSpec):
    #   anthropic → thinking={"type":"enabled","budget_tokens":N}
    #   claude_code → SDK ClaudeAgentOptions.thinking budget
    #   openai/openai-compat → reasoning_effort bucketed from N
    # 0 = unset (defers to thinking_level / the provider default).
    thinking_budget: int = 0
    # Provider-generic thinking LEVEL (#895): off|low|medium|high, or None=unset.
    # 'off' actively disables; None defers to the SHIPPED per-model default --
    # DATA now (claude-code-models.json's shipped_default_effort), never a name heuristic.
    thinking_level: str | None = None
    # Per-provider capability flags. init=False so callers don't need
    # to know they exist; __post_init__ populates them from
    # PROVIDER_DEFAULTS so adding a new wire-protocol quirk = one
    # entry in the defaults dict, no agent.py branches.
    strip_openai_prefix: bool = field(init=False, default=True)
    parse_retry_capability: Literal["bounded", "single_attempt"] = field(
        init=False, default="bounded"
    )
    # Handshake-discovered model config. init=False + default empty so callers
    # never set them; ``apply_handshake`` populates them at bind time (the only
    # place that networks). ``__post_init__`` stays network-free for /health.
    # ``chosen_context`` is the active context limit clio operates against
    # (queryable; for LM Studio it reflects the loaded/load-sized window).
    # ``native_context_window`` is the model's published max from the offline
    # catalog (LiteLLM / bundled model_limits.json); None when unknown.
    context_window: int | None = field(init=False, default=None)
    chosen_context: int | None = field(init=False, default=None)
    native_context_window: int | None = field(init=False, default=None)
    is_reasoning: bool = field(init=False, default=False)
    reasoning_param: str | None = field(init=False, default=None)
    native_tool_calling: bool = field(init=False, default=False)
    tool_call_parser: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Fill empty fields + capability flags from provider defaults."""
        # provider_id is an identity, so it skips kind_default (Part 3); only
        # the no-provider_id form (``LMProviderConfig(provider="argonne")``)
        # resolves via kind.
        identity = self.provider_id or str(self.provider)
        preset = _catalog_provider(identity)
        if preset is None and not self.provider_id:
            preset = _catalog_kind_default(identity)
        if preset is not None:
            self.provider_id = preset.id
            self.provider = cast(Any, preset.provider_kind)
            defaults = _catalog_provider_defaults(preset)
            if self.provider_options:
                self.provider_options = _normalize_provider_options(
                    preset.id, self.provider_options
                )
        else:
            raise ValueError(f"Unknown LM provider {identity!r}; configure a supported provider")
        if self.router_temperature is not None:
            self.planner_temperature = self.router_temperature
        self.router_temperature = self.planner_temperature
        from clio_agent.providers.capabilities.dialects import claude_code  # noqa: PLC0415

        self.thinking_level = claude_code.shipped_default_thinking_level(
            self.provider, self.model, self.thinking_level, self.thinking_budget
        )
        if not self.api_base:
            self.api_base = defaults["api_base"]
        if not self.model:
            self.model = defaults["model"]
        if not self.api_key:
            # Credential resolution (cloud well-known env var / Argonne Globus
            # token) is owned by ``providers.credentials``: read-only, keyed,
            # returned-not-stamped. Delegate for the default ref so the
            # per-expert path and this boot/default path share one resolver.
            # ``or defaults["api_key"]`` preserves the local-provider
            # placeholder (e.g. "lm-studio") — a provider default, not a
            # credential — so behaviour stays byte-identical.
            self.api_key = (
                _credentials.resolve(self.provider_id or self.provider, "") or defaults["api_key"]
            )
        # Zero means no client output cap (#1323).
        if self.max_tokens < 0 or (
            self.planner_max_tokens is not None and self.planner_max_tokens < 0
        ):
            raise ValueError("max_tokens and planner_max_tokens must be non-negative")
        if self.planner_max_tokens is None:
            self.planner_max_tokens = self.max_tokens
        # Capability flags. defaults dict wins — these aren't user-set
        # via env vars (they're wire-protocol facts about the provider),
        # so re-reading on every config load is safe.
        self.strip_openai_prefix = bool(defaults.get("strip_openai_prefix", True))
        self.parse_retry_capability = cast(
            Literal["bounded", "single_attempt"],
            defaults.get("parse_retry_capability", "bounded"),
        )
        if self.codex_transport != "sdk":
            raise ValueError(
                f"codex_transport must use the official Python SDK (got {self.codex_transport!r})"
            )
        if self.claude_code_transport != "sdk":
            raise ValueError(
                "claude_code_transport 'exec' was removed in the v0.8.0 cleanup — "
                f"sdk is the only transport (got {self.claude_code_transport!r})"
            )
        if self.thinking_level is not None:
            from clio_agent.providers import thinking_levels  # noqa: PLC0415

            self.thinking_level = thinking_levels.validate_thinking_level(self.thinking_level)

    def apply_handshake(self, report: Any, *, user_set_max_tokens: bool = False) -> None:
        """Fold a provider handshake report into this config (call at bind time).

        Sets the discovered context window and reasoning/tool capabilities from
        the effective capabilities (model-capabilities brief 5.5), not a flat
        per-provider profile. Output caps remain operator choices: zero omits
        the cap, and positive values are preserved. ``user_set_max_tokens`` is
        retained for callers using the earlier signature. No-op with no usable
        discovered model.

        Context precedence: an explicit ``lm.context_window`` /
        ``CLIO_LM_CONTEXT_WINDOW`` override (>0), else the effective context
        (the smaller of the model's own ceiling and what this deployment is
        actually serving) — logging a ``context_window_below_native`` warning
        when the served window is below the model's native maximum.
        """
        discovered = report.match_model(self.model) if hasattr(report, "match_model") else None
        if discovered is None:
            return

        from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf; lazy
        from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415
        from clio_agent.providers.capabilities.accessor import (
            get_effective_capabilities,  # noqa: PLC0415
        )
        from clio_agent.providers.identity import deployment_key  # noqa: PLC0415

        provider_id, api_base = getattr(report, "provider_id", ""), getattr(report, "api_base", "")
        effective = get_effective_capabilities(provider_id, api_base, discovered.id)
        self.is_reasoning = effective.thinking.known
        self.reasoning_param = effective.thinking.control
        self.native_tool_calling = bool(effective.tools.value)
        self.tool_call_parser = None  # dialect-specific parser naming folds into Part 7

        dep = invalidation.get_deployment_capabilities(
            deployment_key(provider_id, api_base, discovered.id)
        )
        model_key = dep.model_key.value if dep and dep.model_key.known else None
        model = invalidation.get_model_capabilities(model_key) if model_key else None
        self.native_context_window = (
            model.context_max.value if model and model.context_max.known else None
        )
        window = self.context_window = effective.context.value

        # Config override: ``lm.context_window``/``CLIO_LM_CONTEXT_WINDOW`` >0 lets an
        # operator assert a different window than the deployment serves (auto by default).
        # Does NOT suppress the served<native warning below (a config fact, not intent).
        override = conf.resolve(
            "lm.context_window", env="CLIO_LM_CONTEXT_WINDOW", default=0, cast=conf.as_int
        )
        self.chosen_context = override if override and override > 0 else window

        # Warn when the served window is below the model's native max, even
        # with the override in effect — the mismatch is a config fact, not intent.
        if (
            self.native_context_window
            and window is not None
            and window < self.native_context_window
        ):
            logger.warning(
                "context_window_below_native model=%s "
                "served_context=%d native_context=%d "
                "reason=vllm_max_model_len_below_native "
                "hint=set CLIO_LM_CONTEXT_WINDOW or lm.context_window to override",
                self.model,
                window,
                self.native_context_window,
            )
        # Discovery describes capacity; it does not configure an output cap.
        # Keep both zero (uncapped) and explicit per-role limits intact.


def resolve_effective_max_tokens(
    *,
    user_max_tokens: int,
    provider_default: int,
    output_limit: int | None = None,
    context_window: int | None = None,
) -> int:
    """Choose the per-reply output ``max_tokens`` — a real discovered number, no magic.

    Precedence: an explicit ``user_max_tokens`` (>0) wins; otherwise the model's
    discovered maximum output (``output_limit``, e.g. models.dev ``limit.output``)
    when known; otherwise the static ``provider_default`` (unchanged pre-handshake).
    The result is capped to ``context_window`` when known so one reply can never
    exceed the budget. There is deliberately **no** "context minus a prompt reserve"
    arithmetic — that was opaque and overflow-prone. ``max_tokens`` is the output
    cap; the total budget is ``chosen_context``.
    """
    if user_max_tokens and user_max_tokens > 0:
        chosen = int(user_max_tokens)
    elif output_limit and output_limit > 0:
        chosen = int(output_limit)
    else:
        chosen = int(provider_default)
    if context_window and context_window > 0:
        chosen = min(chosen, int(context_window))
    return max(1, chosen)


def _resolve_argonne_api_key() -> str:
    """Return a Globus bearer token for the ALCF inference gateway.

    The implementation now lives in :func:`clio_agent.providers.credentials.
    resolve_argonne_token`. This thin wrapper is retained as the stable seam
    that ``gact.providers.auth`` (runtime token refresh) and tests monkeypatch;
    ``providers.credentials.resolve`` routes the argonne ref back through this
    name so a patch here is observed everywhere.
    """
    return _credentials.resolve_argonne_token()


def load_config_from_env() -> LMProviderConfig:
    """Load LM boot configuration via ``conf`` (file → env → default).

    Every knob resolves with the project precedence: a shared config file
    (``.clio/config.yaml`` / user ``config.yaml``) wins over the matching
    ``CLIO_LM_*`` environment variable, which wins over the in-code provider
    default. See :mod:`clio_agent.conf` for the precedence rationale.

    Config keys → environment variables:
        ``lm.provider`` / CLIO_LM_PROVIDER: Provider name (lm_studio, ollama,
            openai, anthropic, argonne, codex, claude_code)
        ``lm.api_base`` / CLIO_LM_API_BASE: Override API base URL
        ``lm.model`` / CLIO_LM_MODEL: Override model identifier
        ``lm.temperature`` / CLIO_LM_TEMPERATURE: Override reasoner/chat temperature
        ``lm.planner_temperature`` / CLIO_LM_PLANNER_TEMPERATURE: planner temperature
        ``lm.planner_max_tokens`` / CLIO_LM_PLANNER_MAX_TOKENS: planner token cap
        ``lm.max_tokens`` / CLIO_LM_MAX_TOKENS: Override max tokens
        ``lm.top_p`` / ``lm.top_k`` / ``lm.min_p`` / ``lm.presence_penalty``: sampling
        ``lm.codex_transport`` / CLIO_CODEX_TRANSPORT: Codex transport (sdk only)
        ``lm.claude_code_transport`` / CLIO_CLAUDE_CODE_TRANSPORT: Claude Code transport
        ``lm.context_window`` / CLIO_LM_CONTEXT_WINDOW: Override effective context window
            (tokens); 0 = auto-derive from handshake (default). Set to assert a larger
            window than the provider serves (e.g. when vLLM's --max-model-len clips the
            native max). Applied in apply_handshake; resolved lazily there, not here.
        ``runtime.environment`` / CLIO_ENVIRONMENT: Deployment environment

    ``CLIO_LM_API_KEY`` is deliberately **NOT** routed through ``conf``: it is a
    secret and stays env-only (a shared config file must never carry a key). See
    the secret-tier note in :mod:`clio_agent.conf`.

    Returns:
        LMProviderConfig with resolved settings

    Raises:
        ValueError: If cloud provider is selected without API key
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf; lazy per-call

    provider = conf.resolve(
        "lm.provider", env="CLIO_LM_PROVIDER", default="lm_studio", cast=conf.as_str
    )
    api_base = conf.resolve("lm.api_base", env="CLIO_LM_API_BASE", default="", cast=conf.as_str)
    model = conf.resolve("lm.model", env="CLIO_LM_MODEL", default="", cast=conf.as_str)
    # Secret tier: API key stays env-only, never file-resolved (see conf docstring).
    api_key = os.environ.get("CLIO_LM_API_KEY", "")
    environment = conf.resolve(
        "runtime.environment", env="CLIO_ENVIRONMENT", default="dev", cast=conf.as_str
    )
    codex_transport = (
        conf.resolve("lm.codex_transport", env="CLIO_CODEX_TRANSPORT", default="", cast=conf.as_str)
        .strip()
        .lower()
    )
    claude_code_transport = (
        conf.resolve(
            "lm.claude_code_transport",
            env="CLIO_CLAUDE_CODE_TRANSPORT",
            default="",
            cast=conf.as_str,
        )
        .strip()
        .lower()
    )
    # Provider-generic thinking knob (#895): level (off|low|medium|high) and an
    # optional explicit token budget override. Both env-settable so an experiment
    # harness can boot a server at a fixed level without a PUT round-trip.
    thinking_level = (
        conf.resolve(
            "lm.thinking_level", env="CLIO_LM_THINKING_LEVEL", default="", cast=conf.as_str
        )
        .strip()
        .lower()
    )
    thinking_budget = conf.resolve(
        "lm.thinking_budget", env="CLIO_LM_THINKING_BUDGET", default=None, cast=conf.as_int
    )

    # Numeric knobs: default ``None`` means "unset" → the LMProviderConfig
    # provider default applies. cast is applied only to a real file/env value.
    temperature = conf.resolve(
        "lm.temperature", env="CLIO_LM_TEMPERATURE", default=None, cast=conf.as_float
    )
    planner_temperature = conf.resolve(
        "lm.planner_temperature",
        env="CLIO_LM_PLANNER_TEMPERATURE",
        default=None,
        cast=conf.as_float,
    )
    planner_max_tokens = conf.resolve(
        "lm.planner_max_tokens", env="CLIO_LM_PLANNER_MAX_TOKENS", default=None, cast=conf.as_int
    )
    max_tokens = conf.resolve(
        "lm.max_tokens", env="CLIO_LM_MAX_TOKENS", default=None, cast=conf.as_int
    )
    top_p = conf.resolve("lm.top_p", env="CLIO_LM_TOP_P", default=None, cast=conf.as_float)
    top_k = conf.resolve("lm.top_k", env="CLIO_LM_TOP_K", default=None, cast=conf.as_int)
    min_p = conf.resolve("lm.min_p", env="CLIO_LM_MIN_P", default=None, cast=conf.as_float)
    presence_penalty = conf.resolve(
        "lm.presence_penalty", env="CLIO_LM_PRESENCE_PENALTY", default=None, cast=conf.as_float
    )

    kwargs: dict = {
        "provider": provider,
        "provider_id": provider,
        "environment": environment,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if model:
        kwargs["model"] = model
    if api_key:
        kwargs["api_key"] = api_key
    if temperature is not None:
        kwargs["temperature"] = temperature
    if planner_temperature is not None:
        kwargs["planner_temperature"] = planner_temperature
    if planner_max_tokens is not None:
        kwargs["planner_max_tokens"] = planner_max_tokens
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if top_p is not None:
        kwargs["top_p"] = top_p
    if top_k is not None:
        kwargs["top_k"] = top_k
    if min_p is not None:
        kwargs["min_p"] = min_p
    if presence_penalty is not None:
        kwargs["presence_penalty"] = presence_penalty
    if codex_transport:
        kwargs["codex_transport"] = codex_transport
    if claude_code_transport:
        kwargs["claude_code_transport"] = claude_code_transport
    if thinking_level:
        kwargs["thinking_level"] = thinking_level
    if thinking_budget is not None:
        kwargs["thinking_budget"] = thinking_budget

    config = LMProviderConfig(**kwargs)

    # Validate catalog providers that explicitly require an API key.
    selected_provider = _catalog_provider(config.provider_id)
    if selected_provider is not None and selected_provider.requires_api_key and not config.api_key:
        env_var = selected_provider.api_key_env or "CLIO_LM_API_KEY"
        raise ValueError(
            f"Cloud provider '{config.provider_id}' requires an API key. "
            f"Set CLIO_LM_API_KEY or {env_var} environment variable."
        )

    # Argonne / ALCF — token comes from Globus Auth on demand. If
    # both the lazy resolver and the explicit env var came up empty,
    # the user needs to either install globus-sdk + run the OAuth
    # flow once, or pre-mint a token and export it as CLIO_ARGONNE_TOKEN.
    if config.provider == "argonne" and not config.api_key:
        raise ValueError(
            "Argonne / ALCF provider could not obtain a Globus access "
            "token. Either:\n"
            "  1. Install:  pip install 'clio-agent[argonne]'  and run\n"
            "       python -m clio_agent.providers.argonne_auth authenticate\n"
            "  2. Or export CLIO_ARGONNE_TOKEN=<token> directly."
        )

    return config


def has_explicit_model_override(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the LM model was explicitly pinned (file OR env).

    True when either the config-file layer sets a non-empty ``lm.model`` or the
    ``CLIO_LM_MODEL`` environment variable is set. The injected ``env`` mapping
    (used by tests) overrides the process environment for the env-tier check but
    does not affect the file layer.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf; lazy per-call

    file_model = conf.store().file_value("lm.model")
    if isinstance(file_model, str) and file_model.strip():
        return True
    current_env = env if env is not None else os.environ
    return bool(current_env.get("CLIO_LM_MODEL", "").strip())


# ============================================================================
# RE-EXPORTS — runtime LM behavior extracted to clio_agent.lm.* (#769)
# ============================================================================
# config.py owns dotenv, LMProviderConfig (+ resolve_effective_max_tokens),
# load_config_from_env / has_explicit_model_override, setup_dspy +
# is_local_openai_compatible_backend, and the _resolve_argonne_api_key
# monkeypatch seam. Everything else that used to live here now lives in the
# clio_agent.lm.* package and clio_agent.providers.lmstudio_discovery; these
# re-exports keep the historical ``from clio_agent.config import X`` seams (and
# their monkeypatch points) working. New code should import from the owning
# module directly.
# Each line is a plain re-export whose name is imported for side-effect (so
# ``config.<name>`` and the monkeypatch seams keep resolving); ``# noqa: E402,
# F401`` marks the intentional after-code, imported-but-unused re-export.
from clio_agent.lm.adapters import (
    _coerce_constructor_repr_to_jsonable,  # noqa: E402, F401
    _ContextOverflowError,  # noqa: E402, F401
    _dump_unparseable_completion,  # noqa: E402, F401
    _fix_guided_schema,  # noqa: E402, F401
    _guided_output_enabled,  # noqa: E402, F401
    _lenient_chat_adapter_cls,  # noqa: E402, F401
    _live_streaming_enabled,  # noqa: E402, F401
    _parse_retry_attempts,  # noqa: E402, F401
    _recover_malformed_structured_value,  # noqa: E402, F401
    _signature_strict_response_format,  # noqa: E402, F401
    _strict_guided_json_adapter_cls,  # noqa: E402, F401
    _unwrap_self_named_envelope,  # noqa: E402, F401
    create_chat_adapter,  # noqa: E402, F401
)
from clio_agent.lm.factory import (
    _construct_lm,  # noqa: E402, F401
    _ensure_provider_registered,  # noqa: E402, F401
    _is_argonne_sophia,  # noqa: E402, F401
    _resolve_lm_studio_model_if_needed,  # noqa: E402, F401
    _resolve_model_name,  # noqa: E402, F401
    create_lm,  # noqa: E402, F401
    create_planner_lm,  # noqa: E402, F401
)
from clio_agent.lm.io_logging import (
    _TRANSIENT_PROVIDER_MARKERS,  # noqa: E402, F401
    _io_logging_lm_cls,  # noqa: E402, F401
    _is_transient_provider_error,  # noqa: E402, F401
    _lm_transient_backoff_s,  # noqa: E402, F401
    _lm_transient_retries,  # noqa: E402, F401
    _StreamingPlumbingError,  # noqa: E402, F401
    _token_liveness_enabled,  # noqa: E402, F401
)
from clio_agent.lm.request_builder import build_request_kwargs  # noqa: E402, F401
from clio_agent.providers.lmstudio_discovery import (
    LMStudioDiscoveryError,  # noqa: E402, F401
    _openai_compatible_api_base,  # noqa: E402, F401
    list_lm_studio_models,  # noqa: E402, F401
    select_models_for_agents,  # noqa: E402, F401
)


def setup_dspy(model: Optional[str] = None, verbose: bool = True) -> dspy.LM:
    """Setup DSPy with configured LM provider.

    Internally uses load_config_from_env() + create_lm() for provider-agnostic setup.
    Falls back to LM Studio defaults if no environment variables are set.

    Args:
        model: Optional model override
        verbose: If True, print configuration info

    Returns:
        Configured DSPy LM instance

    Example:
        >>> # Use default provider (LM Studio or env-configured)
        >>> lm = setup_dspy()

        >>> # Use with model override
        >>> lm = setup_dspy(model="mistral:7b")
    """
    try:
        config = load_config_from_env()
        if model:
            config.model = model

        lm = create_lm(config)

        if verbose:
            print(f"LM configured ({config.provider})")
            print(f"  API Base: {config.api_base}")
            print(f"  Model: {config.model}")
            print(f"  Temperature: {config.temperature}")
            print(f"  Max Tokens: {config.max_tokens}")

    except ValueError:
        # Config validation error (e.g., missing API key)
        raise
    except Exception as e:
        print(f"\nFailed to configure LM: {e}")
        print("\nTroubleshooting:")
        print("  - Ensure your LM provider is running")
        print("  - Check CLIO_LM_* environment variables")
        raise

    dspy = _dspy()
    # Register the LM-activity callback so the GACT no-progress watchdog can tell
    # an actively-generating (e.g. deep-reasoning) model from a wedged one. A
    # reasoning model can stream tens of thousands of reasoning_content tokens
    # with no answer-content tokens; without this signal the watchdog kills it
    # mid-think. See clio_agent.runtime.lm_activity.
    from clio_agent.runtime.lm_activity import build_dspy_callback  # noqa: PLC0415

    _configure_kwargs: dict[str, Any] = {"lm": lm, "adapter": create_chat_adapter(config)}
    _lm_activity_cb = build_dspy_callback()
    if _lm_activity_cb is not None:
        _configure_kwargs["callbacks"] = [_lm_activity_cb]
    dspy.configure(**_configure_kwargs)

    return lm


def is_local_openai_compatible_backend(config: LMProviderConfig) -> bool:
    """Return whether the configured backend behaves like a local OpenAI API."""
    if config.provider in {"lm_studio", "ollama"}:
        return True
    if config.provider != "openai":
        return False

    parsed = urlparse(config.api_base)
    host = parsed.hostname
    if not host:
        return False
    if host in {"localhost"}:
        return True

    try:
        addr = ip_address(host)
    except ValueError:
        return False

    return addr.is_loopback or addr.is_private or addr.is_link_local


def _is_local_openai_compatible_backend(config: LMProviderConfig) -> bool:
    """Backward-compatible alias for internal callers."""
    return is_local_openai_compatible_backend(config)


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClioAgent Configuration Test")
    print("=" * 60)

    try:
        # Test configuration
        print("\n1. Testing LM configuration...")
        lm = setup_dspy()

        # Simple test prediction
        print("\n2. Testing simple prediction...")
        predictor = _dspy().Predict("question -> answer")
        result = predictor(question="What is 2+2?")
        print(f"Answer: {result.answer}")

        print("\nConfiguration working!")

    except Exception as e:  # noqa: BLE001 - CLI self-test prints the error to the user
        print(f"\nError: {e}")
        print("\nTroubleshooting:")
        print("- Ensure LM provider is running")
        print("- Check CLIO_LM_* environment variables")
