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
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional
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
#   max_tokens (override): 4 096 for ALCF gateway defaults. Live ALCF
#                         model context windows vary by running job, and
#                         some gateway paths reject the shared 32 000
#                         default. Users can override with
#                         CLIO_LM_MAX_TOKENS for larger-context models.
from clio_agent.providers import credentials as _credentials
from clio_agent.providers.catalog import (
    as_cloud_api_key_env as _registry_cloud_api_key_env,
)
from clio_agent.providers.catalog import (
    as_provider_defaults_dict as _registry_provider_defaults,
)

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
        codex_transport: Codex transport: "app_server" (the only transport since v0.8.0)
    """

    provider: Literal[
        "lm_studio",
        "ollama",
        "openai",
        "anthropic",
        "argonne",
        "codex",
        "claude_code",
    ] = "lm_studio"
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    # Default to greedy/deterministic decoding for the agentic LM path.
    # CLIO drives the LM almost exclusively for STRUCTURED output —
    # ReAct tool calls, typed routing decisions, JSON workflow_state,
    # field-formatted DSPy adapter responses. At temperature 1.0 the
    # sampler injects entropy into exactly those structured fields,
    # which is how small/cheap models (and the occasional large one)
    # drift: hallucinated plot columns, fabricated CSV/PNG paths,
    # parse-time field-format breakage. Per DSPy norms, structured/
    # tool-calling predictors want deterministic decoding (temp 0.0)
    # for reproducible, parseable outputs; creativity isn't the job
    # here. Overridable via CLIO_LM_TEMPERATURE / the PUT body / the
    # config field for callers who want sampling.
    temperature: float = 0.0
    # 0 is a sentinel "use the provider's max_tokens override (see
    # PROVIDER_DEFAULTS) if it has one, else 32000". Callers who
    # explicitly pass any non-zero value win.
    max_tokens: int = 0
    planner_temperature: float = 0.3
    planner_max_tokens: int = 0
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
    codex_transport: Literal["app_server"] = "app_server"
    # "sdk" (the only transport since v0.8.0): the in-process Claude Agent SDK
    #   with a persistent CLI session — no per-call spawn, streaming-capable, and
    #   setting_sources=[] keeps the user's ~/.claude/CLAUDE.md out of the prompt.
    #   Needs the claude-agent-sdk package (the `claude-code` extra). The legacy
    #   "exec" batch transport (one `claude -p` per call, ~10-15s cold start,
    #   #715) was deleted in the v0.8.0 cleanup.
    claude_code_transport: Literal["sdk"] = "sdk"
    # Reasoning/thinking budget (explicit token override). Mapped per-provider in
    # create_lm via providers.thinking.resolve_thinking:
    #   anthropic → thinking={"type":"enabled","budget_tokens":N}
    #   claude_code → SDK ClaudeAgentOptions.thinking budget
    #   openai/openai-compat → reasoning_effort bucketed from N
    # 0 = unset (defers to thinking_level / the provider default).
    thinking_budget: int = 0
    # Provider-generic thinking LEVEL (#895): off|low|medium|high, or None=unset.
    # 'off' actively disables; None defers to the SHIPPED per-model default
    # (providers.thinking.shipped_default_level — haiku/claude_code ships 'low').
    thinking_level: str | None = None
    # Per-provider capability flags. init=False so callers don't need
    # to know they exist; __post_init__ populates them from
    # PROVIDER_DEFAULTS so adding a new wire-protocol quirk = one
    # entry in the defaults dict, no agent.py branches.
    strip_openai_prefix: bool = field(init=False, default=True)
    supports_vision: bool = field(init=False, default=False)
    # Handshake-discovered model config. init=False + default empty so callers
    # never set them; ``apply_handshake`` populates them at bind time (the only
    # place that networks). ``__post_init__`` stays network-free for /health.
    # ``chosen_context`` is the active context limit clio operates against
    # (queryable; for LM Studio it reflects the loaded/load-sized window).
    context_window: int | None = field(init=False, default=None)
    chosen_context: int | None = field(init=False, default=None)
    is_reasoning: bool = field(init=False, default=False)
    reasoning_param: str | None = field(init=False, default=None)
    native_tool_calling: bool = field(init=False, default=False)
    tool_call_parser: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Fill empty fields + capability flags from provider defaults."""
        if self.router_temperature is not None:
            self.planner_temperature = self.router_temperature
        self.router_temperature = self.planner_temperature
        defaults = PROVIDER_DEFAULTS.get(self.provider, PROVIDER_DEFAULTS["lm_studio"])
        from clio_agent.providers.thinking import shipped_default_level  # noqa: PLC0415

        self.thinking_level = shipped_default_level(
            self.provider, self.model or "", self.thinking_level, self.thinking_budget
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
            self.api_key = _credentials.resolve(self.provider, "") or defaults["api_key"]
        # max_tokens=0 is the sentinel "pick a sensible default for
        # this provider" — Argonne/ALCF model availability and context
        # windows vary by running gateway job, and some paths reject
        # the global 32000 default.
        if self.max_tokens == 0:
            self.max_tokens = int(defaults.get("max_tokens", 32000))
        self._apply_model_profile_defaults()
        if self.planner_max_tokens == 0:
            self.planner_max_tokens = self.max_tokens
        # Capability flags. defaults dict wins — these aren't user-set
        # via env vars (they're wire-protocol facts about the provider),
        # so re-reading on every config load is safe.
        self.strip_openai_prefix = bool(defaults.get("strip_openai_prefix", True))
        self.supports_vision = bool(defaults.get("supports_vision", False))
        if self.codex_transport != "app_server":
            raise ValueError(
                "codex_transport 'exec'/'sdk' were removed in the v0.8.0 cleanup — "
                f"app_server is the only transport (got {self.codex_transport!r})"
            )
        if self.claude_code_transport != "sdk":
            raise ValueError(
                "claude_code_transport 'exec' was removed in the v0.8.0 cleanup — "
                f"sdk is the only transport (got {self.claude_code_transport!r})"
            )
        if self.thinking_level is not None:
            level = str(self.thinking_level).strip().lower()
            if level not in {"off", "low", "medium", "high"}:
                raise ValueError(
                    f"thinking_level must be off|low|medium|high (got {self.thinking_level!r})"
                )
            self.thinking_level = level

    def _apply_model_profile_defaults(self) -> None:
        """Apply safe defaults for known model families."""
        if not _uses_local_reasoning_model_profile(self.provider, self.model):
            return

        if self.planner_temperature == 0.3:
            self.planner_temperature = 0.0
            self.router_temperature = self.planner_temperature
        if self.planner_max_tokens == 0:
            self.planner_max_tokens = max(self.max_tokens, 4096)
        elif self.planner_max_tokens < 4096:
            self.planner_max_tokens = 4096

    def apply_handshake(self, report: Any, *, user_set_max_tokens: bool = False) -> None:
        """Fold a provider handshake report into this config (call at bind time).

        Sets the discovered per-model fields (context window, reasoning/tool
        capabilities) and, unless the caller explicitly set ``max_tokens``,
        recomputes a context-aware ``max_tokens`` — replacing the static
        provider default (e.g. the ALCF 4096 cap on 256K-context models).
        ``user_set_max_tokens`` must be True when the user/env supplied an
        explicit value so their choice always wins. No-op when the report has no
        usable profile (handshake failed / model not found), preserving today's
        static behaviour.
        """
        profile = None
        models = getattr(report, "models", None) or ()
        if models:
            if hasattr(report, "model"):
                profile = report.model(self.model)
            if profile is None:
                # tolerate vendor-prefix differences (e.g. "gpt-oss-120b" vs
                # "openai/gpt-oss-120b") by matching on the basename.
                want = self.model.rsplit("/", 1)[-1].lower()
                profile = next((m for m in models if m.id.rsplit("/", 1)[-1].lower() == want), None)
            if profile is None and len(models) == 1:
                profile = models[0]
        if profile is None:
            return
        self.context_window = profile.context_window
        self.is_reasoning = bool(profile.is_reasoning)
        self.reasoning_param = profile.reasoning_param
        self.native_tool_calling = bool(profile.native_tool_calling)
        self.tool_call_parser = profile.tool_call_parser
        window = profile.effective_context_window
        self.chosen_context = window
        if not user_set_max_tokens:
            self.max_tokens = resolve_effective_max_tokens(
                user_max_tokens=0,
                provider_default=self.max_tokens,
                output_limit=profile.output_limit,
                context_window=window,
            )
            self.planner_max_tokens = self.max_tokens


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


def _uses_local_reasoning_model_profile(provider: str, model: str) -> bool:
    """Return whether a local model needs reasoning-friendly planner defaults."""
    if provider not in {"lm_studio", "ollama"}:
        return False
    normalized = model.lower().replace("_", "-")
    reasoning_markers = (
        "qwopus",
        "qwen3",
        "qwen-3",
        "qwen35",
        "qwen-3.5",
    )
    return any(marker in normalized for marker in reasoning_markers)


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
        ``lm.codex_transport`` / CLIO_CODEX_TRANSPORT: Codex transport (app_server only)
        ``lm.claude_code_transport`` / CLIO_CLAUDE_CODE_TRANSPORT: Claude Code transport
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

    # Validate cloud providers have API keys
    if config.provider in ("openai", "anthropic") and not config.api_key:
        env_var = _CLOUD_API_KEY_ENV[config.provider]
        raise ValueError(
            f"Cloud provider '{config.provider}' requires an API key. "
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
    _dump_unparseable_completion,  # noqa: E402, F401
    _fix_guided_schema,  # noqa: E402, F401
    _guided_output_enabled,  # noqa: E402, F401
    _lenient_chat_adapter_cls,  # noqa: E402, F401
    _live_streaming_enabled,  # noqa: E402, F401
    _parse_retry_attempts,  # noqa: E402, F401
    _reasoning_model_capability,  # noqa: E402, F401
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
    _provider_lm_kwargs,  # noqa: E402, F401
    _resolve_lm_studio_model_if_needed,  # noqa: E402, F401
    _resolve_model_name,  # noqa: E402, F401
    _thinking_disabled,  # noqa: E402, F401
    _thinking_kwargs,  # noqa: E402, F401
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
