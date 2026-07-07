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
from typing import TYPE_CHECKING, Any, List, Literal, Mapping, Optional
from urllib.parse import urlparse

from clio_agent.runtime.stream_audit import stream_audit

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
# PROVIDER DEFAULTS — derived from clio_agent.providers.registry
# ============================================================================

#
# These two dicts are derived views over the canonical provider list at
# ``src/clio_agent/providers/registry.py``. Add a new provider by adding
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
from clio_agent.providers.registry import (
    as_cloud_api_key_env as _registry_cloud_api_key_env,
)
from clio_agent.providers.registry import (
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
        codex_transport: Codex transport mode, either "exec" or "sdk"
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
    codex_transport: Literal["exec", "sdk"] = "exec"
    # "sdk" (DEFAULT — the best config): the in-process Claude Agent SDK with a
    #   persistent CLI session — no per-call spawn, streaming-capable, and
    #   setting_sources=[] keeps the user's ~/.claude/CLAUDE.md out of the prompt
    #   (faster + cleaner than exec). Needs the claude-agent-sdk package (the
    #   `claude-code` extra).
    # "exec": one `claude -p` subprocess per LM call — needs only the `claude` CLI on
    #   PATH, but pays ~10-15s cold start every call (#715). Explicit opt-out.
    claude_code_transport: Literal["exec", "sdk"] = "sdk"
    # Reasoning/thinking budget. Mapped per-provider in create_lm:
    #   anthropic → thinking={"type":"enabled","budget_tokens":N}
    #   openai/openai-compat → reasoning_effort bucketed from N
    # 0 disables.
    thinking_budget: int = 0
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
        if self.codex_transport not in {"exec", "sdk"}:
            raise ValueError(
                f"codex_transport must be 'exec' or 'sdk' (got {self.codex_transport!r})"
            )
        if self.claude_code_transport not in {"exec", "sdk"}:
            raise ValueError(
                f"claude_code_transport must be 'exec' or 'sdk' "
                f"(got {self.claude_code_transport!r})"
            )

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
            (legacy CLIO_LM_ROUTER_TEMPERATURE is honored env-tier only)
        ``lm.planner_max_tokens`` / CLIO_LM_PLANNER_MAX_TOKENS: planner token cap
        ``lm.max_tokens`` / CLIO_LM_MAX_TOKENS: Override max tokens
        ``lm.top_p`` / ``lm.top_k`` / ``lm.min_p`` / ``lm.presence_penalty``: sampling
        ``lm.codex_transport`` / CLIO_CODEX_TRANSPORT: Codex transport (exec or sdk)
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
    codex_transport = conf.resolve(
        "lm.codex_transport", env="CLIO_CODEX_TRANSPORT", default="", cast=conf.as_str
    ).strip().lower()
    claude_code_transport = conf.resolve(
        "lm.claude_code_transport", env="CLIO_CLAUDE_CODE_TRANSPORT", default="", cast=conf.as_str
    ).strip().lower()

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
    if planner_temperature is None:
        # Legacy alias, env-tier only (below file + CLIO_LM_PLANNER_TEMPERATURE).
        router_temperature = os.environ.get("CLIO_LM_ROUTER_TEMPERATURE", "")
        if router_temperature.strip():
            planner_temperature = conf.as_float(router_temperature)
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


class _StreamingPlumbingError(Exception):
    """Internal: token-liveness streaming could not be set up (anyio/dspy
    unavailable, or an event-loop plumbing fault). Signals ``IOLoggingLM.__call__``
    to fall back to the blocking call WITHOUT a second LM round-trip. Never
    raised for a real LM/provider error -- those propagate to the repair loop."""


def _token_liveness_enabled() -> bool:
    """Whether expert LM calls stream so each token refreshes the no-progress
    watchdog (token-liveness). Default ON; kill switch CLIO_LM_TOKEN_LIVENESS=0.

    The mechanism (see IOLoggingLM._clio_streamed_call) only engages for
    synchronous calls outside a running event loop -- i.e. the executor-run expert
    calls -- and defers to the normal blocking path everywhere else.

    Force-OFF under guided output: a guided/structured response streams as
    ``reasoning_content``-only deltas (no ``content`` deltas) on LM Studio, which
    the stream assembly can't fold into content -> empty content -> parse failure.
    The blocking path applies the ``content<-reasoning_content`` fallback
    (``_process_completion``), so guided output uses blocking calls. (TODO: fold
    reasoning deltas into the stream assembly to re-enable liveness here.)
    """
    if _guided_output_enabled():
        return False
    try:
        from clio_agent.conf import as_bool, resolve  # noqa: PLC0415

        return bool(
            resolve(
                "runtime.lm_token_liveness",
                env="CLIO_LM_TOKEN_LIVENESS",
                default=True,
                cast=as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break LM construction; default on
        return True


# Substrings (matched against the exception's class-name chain AND its message)
# that identify a TRANSIENT provider failure worth retrying: a local model process
# crashing mid-inference (LM Studio "the model has crashed" -> MidStreamFallbackError),
# a dropped connection, a 503/overloaded backend, or a request timeout. Typed-output
# / adapter-parse / validation errors are deliberately ABSENT -- those are not
# transient; the extract/repair loop owns them and they must not be retried here.
_TRANSIENT_PROVIDER_MARKERS = (
    "midstreamfallback",
    "apiconnectionerror",
    "serviceunavailable",
    "internalservererror",
    "apitimeouterror",
    "timeout",  # httpx ReadTimeout/ConnectTimeout/TimeoutException, litellm.Timeout
    "the model has crashed",
    "connection error",
    "remote end closed connection",
    "connection reset",
    "overloaded",
)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True for transient provider/infrastructure failures that a re-issue can heal
    (vs. typed-output/parse errors, which are the repair loop's job, not retried)."""
    names = " ".join(base.__name__.lower() for base in type(exc).__mro__)
    text = f"{names} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_PROVIDER_MARKERS)


def _lm_transient_retries() -> int:
    """Bounded retries for a transient provider failure (default 2)."""
    try:
        from clio_agent.conf import as_float, resolve  # noqa: PLC0415

        return max(
            0,
            int(
                resolve(
                    "limits.lm_transient_retries",
                    env="CLIO_LM_TRANSIENT_RETRIES",
                    default=2.0,
                    cast=as_float,
                )
            ),
        )
    except Exception:  # noqa: BLE001 - never let config break a call
        return 2


def _lm_transient_backoff_s() -> float:
    """Backoff before re-issuing after a transient failure (default 8s -- enough for
    LM Studio to JIT-reload a crashed local model on the next request)."""
    try:
        from clio_agent.conf import as_float, resolve  # noqa: PLC0415

        value = resolve(
            "limits.lm_transient_backoff_s",
            env="CLIO_LM_TRANSIENT_BACKOFF_S",
            default=8.0,
            cast=as_float,
        )
        return value if value >= 0 else 8.0
    except Exception:  # noqa: BLE001 - never let config break a call
        return 8.0


_IO_LOGGING_LM_CLS: Any = None


def _io_logging_lm_cls() -> Any:
    """Build (once) a dspy.LM subclass that logs every call's full I/O."""
    global _IO_LOGGING_LM_CLS  # noqa: PLW0603
    if _IO_LOGGING_LM_CLS is not None:
        return _IO_LOGGING_LM_CLS
    dspy = _dspy()

    class IOLoggingLM(dspy.LM):  # type: ignore[name-defined,misc]
        """dspy.LM that emits a durable ``lm.call`` trace event per call.

        Reads ``history[-1]`` after each call (same thread as the call), so it
        captures the raw ``content`` AND ``reasoning_content`` channels even when
        the response was truncated or failed downstream parsing -- the one place
        an expert call's reasoning is reliably visible (expert LMs run in
        executors the settle path cannot reach). The happy path is unchanged.
        The canonical trace is the single recorder (no separate JSONL mirror).
        """

        def __call__(self, prompt=None, messages=None, **kwargs):  # type: ignore[override]
            # LM Studio response_format shim (guided output only). DSPy's
            # JSONAdapter sends ``response_format={"type":"json_object"}`` for any
            # signature with an open-ended field (qwopus experts: main's
            # delegation/workflow_state, ReAct's next_tool_args:dict). LM Studio
            # REJECTS json_object ("'response_format.type' must be 'json_schema'
            # or 'text'"), 400-ing the call. Translate it to a permissive
            # json_schema (constrain to a valid JSON object -- json_object
            # semantics -- in the form LM Studio accepts). Strict per-signature
            # schemas (clean signatures) already flow through as pydantic models
            # and are untouched. No-op when guided output is off.
            if _guided_output_enabled():
                _rf = kwargs.get("response_format")
                if isinstance(_rf, dict) and _rf.get("type") == "json_object":
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "output",
                            "strict": False,
                            "schema": {"type": "object", "additionalProperties": True},
                        },
                    }
            # Bounded retry on TRANSIENT provider failures -- e.g. a local model
            # process crashing mid-inference (LM Studio "the model has crashed" ->
            # MidStreamFallbackError), a dropped connection, or a 503. These abort a
            # turn that is otherwise healthy (here: the parent crashed while routing,
            # AFTER the catalog had already ranked 71 stations). A short backoff lets
            # the provider JIT-reload the crashed model and the call is re-issued.
            # Typed-output/parse/validation errors are NOT transient -- the extract
            # repair loop owns those -- and propagate immediately on the first try.
            attempts = _lm_transient_retries() + 1
            last_exc: BaseException | None = None
            for attempt in range(attempts):
                try:
                    return self._clio_invoke_once(prompt, messages, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised unless transient
                    last_exc = exc
                    if attempt + 1 < attempts and _is_transient_provider_error(exc):
                        import time as _time  # noqa: PLC0415

                        _time.sleep(_lm_transient_backoff_s())
                        continue
                    raise
            assert last_exc is not None  # unreachable; loop returns or raises
            raise last_exc

        def _clio_invoke_once(self, prompt=None, messages=None, **kwargs):  # type: ignore[no-untyped-def]
            # Token-streaming liveness: when enabled AND this call is synchronous
            # (outside a running event loop -- i.e. an executor-run expert call),
            # drive it streamed so each chunk refreshes the no-progress watchdog.
            # In a running loop (e.g. the Tier-1 streamify path) we defer to the
            # blocking call below so we never nest loops / double-stream. Either
            # path emits the canonical lm.call once via the shared finally.
            try:
                if _token_liveness_enabled() and self._clio_can_stream():
                    try:
                        return self._clio_streamed_call(prompt, messages, **kwargs)
                    finally:
                        self._clio_log_last_call()
            except _StreamingPlumbingError:
                pass  # streaming setup unavailable -> fall through to blocking
            try:
                return super().__call__(prompt=prompt, messages=messages, **kwargs)
            finally:
                self._clio_log_last_call()

        def _process_completion(self, response, merged_kwargs):  # type: ignore[no-untyped-def]
            # Reasoning-model content<-reasoning_content fallback. Reasoning models
            # (qwopus, nemotron, ...) intermittently emit the FULL formatted output
            # into the `reasoning_content` channel and leave `content` EMPTY. dspy's
            # base adapter parses output["text"] (= content); empty -> {} -> every
            # field missing -> ValidationError/AdapterParseError. This is the
            # confirmed dominant cause of qwopus typed-output intermittency (verified
            # live: a json_schema call returned schema-perfect JSON in
            # reasoning_content with content=""). When text is empty but
            # reasoning_content is present, use reasoning_content as the parse text so
            # the adapter parses the actual output (the LenientChatAdapter's
            # json/constructor-repr repair then handles its shape). Normal calls
            # (non-empty content) are untouched.
            outputs = super()._process_completion(response, merged_kwargs)
            # Per-model: only reasoning models (qwopus/qwen ...) route output into
            # reasoning_content and need this extraction. Non-reasoning models never
            # leave content empty, so this is a no-op for them, but gate it
            # explicitly per model (set in create_lm) rather than running globally.
            if not getattr(self, "_clio_reasoning_fallback", True):
                return outputs
            try:
                choices = list(getattr(response, "choices", None) or [])
            except Exception:  # noqa: BLE001 - defensive; fall back to no finish info
                choices = []
            # A legitimate formatted output (reasoning + answer + workflow_state) is a
            # few KB; a runaway/truncated chain-of-thought is 100k+ chars. Substituting
            # a truncated giant CoT as the "output" both fails to parse AND bloats every
            # downstream prompt (observed: a 132k-char finish='length' reasoning blew a
            # delegation output to 280k -> 71k-token prompt -> context overflow). So only
            # fall back to reasoning_content when the response COMPLETED normally
            # (finish != 'length') and is sanely sized.
            _MAX_REASONING_FALLBACK_CHARS = 48000
            patched = []
            for i, out in enumerate(outputs):
                if isinstance(out, dict):
                    text = (out.get("text") or "").strip()
                    rc = out.get("reasoning_content") or ""
                    finish = ""
                    if i < len(choices):
                        ch = choices[i]
                        finish = str(
                            getattr(ch, "finish_reason", None)
                            or (ch.get("finish_reason") if isinstance(ch, dict) else "")
                            or ""
                        )
                    if (
                        not text
                        and rc.strip()
                        and finish != "length"
                        and len(rc) <= _MAX_REASONING_FALLBACK_CHARS
                    ):
                        out = {**out, "text": rc}
                patched.append(out)
            return patched

        @staticmethod
        def _clio_can_stream() -> bool:
            """True only when NOT inside a running event loop.

            ``_clio_streamed_call`` uses ``asyncio.run`` (it owns a fresh loop), so
            it applies to the synchronous executor expert calls and defers to the
            normal blocking path inside any already-running loop.
            """
            import asyncio as _asyncio  # noqa: PLC0415

            try:
                _asyncio.get_running_loop()
            except RuntimeError:
                return True
            return False

        def _clio_streamed_call(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
            """Run the call STREAMED so each chunk refreshes the watchdog.

            Producer awaits ``self.acall`` with ``dspy.settings.send_stream``
            set; a consumer drains-and-discards each chunk, calling
            ``note_lm_activity`` per token. ``acall`` (NOT ``aforward``) is the
            ``@with_callbacks``-wrapped entry: it fires ``on_lm_start``/``on_lm_end``
            -> ``note_lm_start``/``note_lm_end`` (so the call registers as in-flight
            for the watchdog) + the ``lm.call.started`` marker, and it returns the
            SAME processed outputs as the blocking ``__call__`` (``aforward`` +
            ``_process_lm_response``). The inner ``aforward`` assembles the
            authoritative result (litellm ``stream_chunk_builder``) and updates
            ``self.history`` -- so the shared ``_clio_log_last_call`` finally still
            emits ``lm.call``.

            Real LM errors (raised inside ``aforward``) propagate so the repair loop
            handles them exactly as on the blocking path. Streaming-PLUMBING failures
            (anyio/dspy unavailable) raise ``_StreamingPlumbingError`` so ``__call__``
            falls back to the blocking call -- without a double LM round-trip.

            Version-fragile (public surfaces): dspy.BaseLM.acall (@with_callbacks)
            + dspy.settings send_stream + litellm streaming; anyio memory object
            streams. Gated default-on with the CLIO_LM_TOKEN_LIVENESS kill switch.
            """
            import asyncio as _asyncio  # noqa: PLC0415

            try:
                import time as _time  # noqa: PLC0415

                import anyio as _anyio  # noqa: PLC0415

                from clio_agent.runtime import trace  # noqa: PLC0415
                from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                    note_lm_activity,
                    note_lm_answer_delta,
                    note_lm_token_event,
                )
                from clio_agent.runtime.lm_stream import (  # noqa: PLC0415
                    AnswerFieldExtractor,
                    extract_delta,
                )
            except Exception as exc:  # noqa: BLE001 - plumbing missing -> blocking
                raise _StreamingPlumbingError from exc

            dspy = _dspy()

            async def _drive() -> Any:
                send, recv = _anyio.create_memory_object_stream(float("inf"))
                holder: dict[str, Any] = {}

                async def _produce() -> None:
                    try:
                        with dspy.settings.context(send_stream=send):
                            holder["result"] = await self.acall(
                                prompt=prompt, messages=messages, **kwargs
                            )
                    except BaseException as exc:  # noqa: BLE001 - re-raised post-drain
                        holder["exc"] = exc
                    finally:
                        await send.aclose()

                extractors = {
                    "reasoning": AnswerFieldExtractor("reasoning"),
                    "answer": AnswerFieldExtractor("answer"),
                    "next_thought": AnswerFieldExtractor("next_thought"),
                    "next_expert": AnswerFieldExtractor("next_expert"),
                    "next_task": AnswerFieldExtractor("next_task"),
                    "workflow_state": AnswerFieldExtractor("workflow_state"),
                }
                visible_contract_fields = {"reasoning", "next_thought", "answer"}
                acc_answer = ""
                acc_reasoning = ""
                last_event = _time.monotonic()
                async with _anyio.create_task_group() as tg:
                    tg.start_soon(_produce)
                    async with recv:
                        async for _chunk in recv:
                            note_lm_activity()  # watchdog liveness (per token)
                            content, reasoning = extract_delta(_chunk)
                            if content:
                                trace.HF_ON and trace.hot(
                                    "STREAM-FIELD",
                                    "raw_content len=%d head=%r",
                                    len(content),
                                    content[:120],
                                )
                                for field_name, extractor in extractors.items():
                                    answer_delta = extractor.feed(content)
                                    if (
                                        answer_delta
                                        and field_name in visible_contract_fields
                                        and not extractor.is_structured()
                                    ):
                                        trace.HF_ON and trace.hot(
                                            "STREAM-FIELD",
                                            "emit field=%s len=%d head=%r",
                                            field_name,
                                            len(answer_delta),
                                            answer_delta[:120],
                                        )
                                        # Preserve exact generated output-field tokens.
                                        note_lm_answer_delta(answer_delta, field=field_name)
                                        acc_answer += answer_delta  # highway gets all
                            if reasoning:
                                acc_reasoning += reasoning
                            # highway event (trace + ARC), coalesced so the durable
                            # stream isn't one event per token.
                            now = _time.monotonic()
                            if now - last_event >= 0.25 and (acc_answer or acc_reasoning):
                                note_lm_token_event(acc_answer, acc_reasoning)
                                acc_answer = ""
                                acc_reasoning = ""
                                last_event = now
                    for field_name, extractor in extractors.items():
                        tail = extractor.flush()
                        if (
                            tail
                            and field_name in visible_contract_fields
                            and not extractor.is_structured()
                        ):
                            trace.HF_ON and trace.hot(
                                "STREAM-FIELD",
                                "flush field=%s len=%d head=%r",
                                field_name,
                                len(tail),
                                tail[:120],
                            )
                            note_lm_answer_delta(tail, field=field_name)
                            acc_answer += tail
                    if acc_answer or acc_reasoning:
                        note_lm_token_event(acc_answer, acc_reasoning)
                if "exc" in holder:
                    raise holder["exc"]
                return holder.get("result")

            try:
                return _asyncio.run(_drive())
            except _StreamingPlumbingError:
                raise
            except BaseException as exc:
                # aforward's own error -> propagate (repair loop owns it). A bare
                # asyncio/anyio plumbing failure also lands here; treat anything
                # that is clearly a loop/runtime plumbing fault as fall-back-able,
                # else propagate so a genuine LM failure is not swallowed.
                if isinstance(exc, RuntimeError) and "loop" in str(exc).lower():
                    raise _StreamingPlumbingError from exc
                raise

        @staticmethod
        def _clio_trace_target() -> Any:
            """Return the active GACT trace target (app, sid, turn, trace, emit)
            or None. Lazily imports app to avoid an import cycle; resolves the
            turn-scoped contextvars copied into the executor running this call."""
            try:
                from clio_agent.gact.context import (  # noqa: PLC0415
                    active_app,
                    active_session_id,
                    active_trace_id,
                    active_turn_id,
                )
                from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - app may be unavailable (CLI/optimizer paths)
                return None
            app = active_app()
            sid = active_session_id()
            if app is None or not sid:
                return None
            return (
                app,
                sid,
                active_turn_id(),
                active_trace_id(),
                _emit_semantic_event,
            )

        def _clio_log_last_call(self) -> None:
            try:
                # ONE capture per call. Read ``history[-1]`` exactly once here and
                # stash the reasoning-channel text on the instance so the ReAct loop
                # reuses THIS read (``app._active_lm_last_reasoning``) instead of a
                # second independent ``history[-1]`` read. Done before the trace gate
                # so the stash is populated for every call (the loop runs inside a
                # GACT turn; a non-turn call simply emits no ``lm.call``).
                history = getattr(self, "history", None) or []
                if not history or not isinstance(history[-1], dict):
                    return
                entry = history[-1]
                response = entry.get("response")
                content = reasoning = finish = ""
                choices = getattr(response, "choices", None)
                if choices is None and isinstance(response, dict):
                    choices = response.get("choices")
                if choices:
                    ch0 = choices[0]
                    msg = getattr(ch0, "message", None)
                    if msg is None and isinstance(ch0, dict):
                        msg = ch0.get("message")
                    if msg is not None:
                        content = (
                            getattr(msg, "content", None)
                            if not isinstance(msg, dict)
                            else msg.get("content")
                        ) or ""
                        reasoning = (
                            getattr(msg, "reasoning_content", None)
                            if not isinstance(msg, dict)
                            else msg.get("reasoning_content")
                        ) or ""
                    finish = (
                        getattr(ch0, "finish_reason", None)
                        if not isinstance(ch0, dict)
                        else ch0.get("finish_reason")
                    ) or ""
                # Stash the reasoning from this single read so the react step reuses it.
                self._clio_last_reasoning = str(reasoning or "").strip()
                record = {
                    "model": entry.get("model"),
                    "messages": entry.get("messages") or entry.get("prompt"),
                    "content": content,
                    "content_len": len(str(content)),
                    "reasoning_content": reasoning,
                    "reasoning_len": len(str(reasoning)),
                    "finish_reason": finish,
                    "usage": entry.get("usage"),
                    "timestamp": entry.get("timestamp"),
                }
                try:
                    from clio_agent.gact.context import (  # noqa: PLC0415
                        active_session_id,
                        active_trace_id,
                        active_turn_id,
                    )

                    audit_sid = active_session_id()
                    audit_turn_id = active_turn_id()
                    audit_trace_id = active_trace_id()
                except Exception:  # noqa: BLE001 - audit is best-effort
                    audit_sid = ""
                    audit_turn_id = ""
                    audit_trace_id = ""
                stream_audit(
                    "provider.batch_response",
                    provider="dspy_lm",
                    session_id=audit_sid,
                    turn_id=audit_turn_id,
                    trace_id=audit_trace_id,
                    model=str(record["model"] or ""),
                    source_channel=(
                        "content+reasoning_content"
                        if content and reasoning
                        else ("reasoning_content" if reasoning else "content")
                    ),
                    content_len=len(str(content)),
                    reasoning_len=len(str(reasoning)),
                    chunk_len=len(str(content or reasoning)),
                    finish_reason=finish,
                    head=str(content or reasoning)[:120],
                )
                # No active GACT turn -> nothing to emit (CLI/optimizer paths). The
                # stash above is still set so a synchronous loop can read it, and the
                # batch provider audit above still records timing when enabled.
                target = self._clio_trace_target()
                if target is None:
                    return
                # Emit the canonical trace's DURABLE-ONLY lm.call event: the one
                # place an expert call's raw messages + reasoning_content are
                # reliably visible (expert LMs run in executors the settle path
                # can't reach), captured on the failure path too. detail_level="off"
                # keeps it off SSE/UI. (Legacy CLIO_LOG_LM_IO JSONL mirror removed --
                # the canonical trace is the single recorder.)
                app, sid, turn_id, trace_id, emit = target
                try:
                    emit(
                        app,
                        sid,
                        "lm.call",
                        turn_id=turn_id,
                        trace_id=trace_id,
                        status="completed",
                        summary=f"LM call ({record['finish_reason'] or 'ok'}).",
                        provider={"model_id": str(record["model"] or "")},
                        payload=record,
                        detail_level="off",
                    )
                except Exception as exc:  # noqa: BLE001 - capture must never fail a call
                    # NEVER silent: surfaces e.g. the ARC-as-source fail-loud RuntimeError
                    # (no ARC reachable) without breaking the call.
                    from clio_agent.runtime import trace  # noqa: PLC0415

                    trace.event("LM-CALL-CAPTURE", "lm.call capture/emit failed: %r", exc)
            except Exception as exc:  # noqa: BLE001 - logging is best-effort, never fail a call
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event("LM-CALL-CAPTURE", "lm.call logging failed: %r", exc)

    _IO_LOGGING_LM_CLS = IOLoggingLM
    return _IO_LOGGING_LM_CLS


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
    except Exception:  # noqa: BLE001 - never let tagging break LM construction
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


def _coerce_constructor_repr_to_jsonable(text: str) -> Any:
    """Coerce a Python constructor-repr into a nested dict/list/scalar.

    Some local reasoning models (e.g. qwopus) emit a typed output field as a
    Python constructor call — ``Model(field=val, nested=Sub(a=1, b=[...]))`` —
    instead of JSON, which no DSPy adapter parses. This rewrites that shape into
    plain JSON-able data using ``ast`` (constructor calls -> dicts keyed by their
    keyword args; lists/tuples/sets -> lists; literals as-is). Raises on anything
    that is not such a repr, so the caller can fall back to the original error.
    """
    import ast  # noqa: PLC0415

    node = ast.parse(text.strip(), mode="eval").body

    def conv(n: Any) -> Any:
        if isinstance(n, ast.Call):
            return {kw.arg: conv(kw.value) for kw in n.keywords if kw.arg is not None}
        if isinstance(n, ast.Dict):
            return {conv(k): conv(v) for k, v in zip(n.keys, n.values, strict=False)}
        if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            return [conv(e) for e in n.elts]
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -conv(n.operand)
        if isinstance(n, ast.Name):
            # A bare identifier where a value was expected. Local models (qwopus)
            # routinely emit unquoted JS-style literals or unquoted string values
            # inside a constructor-repr -- e.g. ``analysis_ready=true`` (JS literal,
            # not Python ``True``) or ``status=staged`` (unquoted string). Python's
            # ast sees these as Name nodes, which ast.literal_eval rejects ("malformed
            # node ... ast.Name") and the whole staging tool-call dies. Map the JS
            # literals and treat any other bare name as its string value. Format-only.
            low = n.id.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            if low in ("none", "null"):
                return None
            return n.id
        if isinstance(n, ast.Attribute):
            # e.g. an enum-ish ``Status.STAGED`` -> use the trailing attribute name.
            return n.attr
        return ast.literal_eval(n)

    return conv(node)


def _unwrap_self_named_envelope(obj: Any, field_name: str) -> Any:
    """Unwrap a structured value a model framed under its own field name.

    Reasoning/small models routinely emit a structured output field's value
    wrapped in a single-key envelope keyed by the field's own name -- e.g. the
    ``workflow_state`` field returned as ``{"workflow_state": {...}}`` instead of
    just ``{...}`` (qwopus copies this shape straight from blueprint examples).
    This is a framing error with no semantic change, so unwrap it. Only triggers
    when the dict has exactly that one key and a structured (dict/list) inner
    value -- never alters a genuine single-key payload of a different name.
    """
    if (
        isinstance(obj, dict)
        and len(obj) == 1
        and field_name in obj
        and isinstance(obj[field_name], (dict, list))
    ):
        return obj[field_name]
    return obj


def _recover_malformed_structured_value(field_name: str, text: str) -> Any:
    """Recover a structured field value from a model's malformed text.

    Handles, in order, the format errors local models produce on JSON-object
    output fields -- all purely structural, no semantic change:

    1. a dropped/extra brace or bracket (``json_repair`` rebalances it),
    2. a Python constructor-repr (``Model(field=...)``) instead of JSON
       (``_coerce_constructor_repr_to_jsonable``),
    3. a self-named envelope (``{"<field>": {...}}``) -- unwrapped.

    Raises if none apply, so the caller can dump + surface the original error.
    """
    import json as _json  # noqa: PLC0415

    obj: Any = None
    try:
        import json_repair  # noqa: PLC0415

        obj = _json.loads(json_repair.repair_json(text))
    except Exception:  # noqa: BLE001 - fall through to constructor-repr
        obj = None
    if obj is None:
        obj = _coerce_constructor_repr_to_jsonable(text)
    return _unwrap_self_named_envelope(obj, field_name)


def _dump_unparseable_completion(
    signature: Any, completion: str, field: str, value: str, error: str
) -> None:
    """Best-effort diagnostic dump of a model completion the adapter could not parse.

    Captures the raw ``content`` the strict + lenient parsers both rejected, plus the
    specific field value that broke, so the model↔adapter format mismatch can be seen
    directly instead of inferred. Gated by ``CLIO_DUMP_UNPARSEABLE`` (a file path);
    no-op when unset. Never raises.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    path = conf.resolve(
        "debug.dump_unparseable", env="CLIO_DUMP_UNPARSEABLE", default="", cast=conf.as_str
    ).strip()
    if not path:
        return
    try:
        import json as _json  # noqa: PLC0415

        record = {
            "signature": getattr(signature, "__name__", str(signature)),
            "output_fields": list(getattr(signature, "output_fields", {}).keys()),
            "failing_field": field,
            "error": error,
            "failing_value": value,
            "raw_completion": completion,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fail a turn
        logger.warning(
            "unparseable-output dump not written "
            "reason=unparseable_dump_write_failed path=%s error=%s",
            path,
            exc,
        )


_LENIENT_CHAT_ADAPTER_CLS: Any = None


def _lenient_chat_adapter_cls() -> Any:
    """Build (once) a ChatAdapter subclass that recovers constructor-repr fields."""
    global _LENIENT_CHAT_ADAPTER_CLS  # noqa: PLW0603
    if _LENIENT_CHAT_ADAPTER_CLS is not None:
        return _LENIENT_CHAT_ADAPTER_CLS
    dspy = _dspy()
    import json as _json  # noqa: PLC0415

    from dspy.adapters.chat_adapter import field_header_pattern  # noqa: PLC0415
    from dspy.adapters.utils import parse_value  # noqa: PLC0415
    from dspy.utils.exceptions import AdapterParseError  # noqa: PLC0415

    class LenientChatAdapter(dspy.ChatAdapter):  # type: ignore[name-defined]
        """ChatAdapter that recovers a structured output field a model emitted as a
        Python constructor-repr (``Model(field=...)``) instead of JSON. The happy
        path is unchanged; recovery only runs when the strict parse fails."""

        def parse(self, signature: Any, completion: str) -> dict:
            try:
                return super().parse(signature, completion)
            except Exception as primary_exc:
                # Re-section exactly like ChatAdapter, but coerce a failing field's
                # constructor-repr value into JSON before re-parsing it.
                sections: list[tuple[Any, list[str]]] = [(None, [])]
                for line in completion.splitlines():
                    match = field_header_pattern.match(line.strip())
                    if match:
                        header = match.group(1)
                        remaining = line[match.end() :].strip()
                        sections.append((header, [remaining] if remaining else []))
                    else:
                        sections[-1][1].append(line)
                collapsed = [(k, "\n".join(v).strip()) for k, v in sections]
                fields: dict[str, Any] = {}
                recovered_fields: list[str] = []
                for k, v in collapsed:
                    if k in signature.output_fields and k not in fields:
                        annotation = signature.output_fields[k].annotation
                        try:
                            fields[k] = parse_value(v, annotation)
                        except Exception:
                            # Recover structural malformations local models produce on
                            # JSON-object fields -- a dropped brace, a constructor-repr,
                            # or a self-named envelope ({"workflow_state": {...}}). All
                            # format-only (no semantic change); see
                            # _recover_malformed_structured_value.
                            try:
                                recovered = _recover_malformed_structured_value(str(k), v)
                            except Exception as recover_exc:
                                _dump_unparseable_completion(
                                    signature, completion, str(k), v, str(recover_exc)
                                )
                                raise
                            fields[k] = parse_value(_json.dumps(recovered, default=str), annotation)
                            recovered_fields.append(str(k))
                if fields.keys() != signature.output_fields.keys():
                    raise  # genuinely missing fields — keep the original error
                # Loud trace flag so the semantics are visible: this turn's output
                # was NOT valid for the strict parser and was recovered from a
                # constructor-repr. If you see this a lot, the model isn't emitting
                # JSON natively (a root issue worth fixing upstream, not just here).
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event(
                    "LENIENT-ADAPTER RECOVERY",
                    "coerced constructor-repr -> JSON for field(s) %s (strict parse failed: %s)",
                    recovered_fields,
                    str(primary_exc)[:120],
                )
                return fields

        def _clio_resample_attempts(self) -> int:
            return int(getattr(self, "_clio_parse_retry", 0) or 0)

        @staticmethod
        def _clio_trace_resample(attempt: int, exc: Exception) -> None:
            from clio_agent.runtime import trace  # noqa: PLC0415

            trace.event(
                "ADAPTER-RESAMPLE",
                "parse failed (attempt %d), re-sampling the LM call: %s",
                attempt + 1,
                str(exc)[:160],
            )

        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            # Bounded re-SAMPLE on an unrecoverable parse failure. The lenient
            # parse() above repairs SHAPE (constructor-repr, dropped brace); it
            # CANNOT recover a genuinely missing field -- e.g. a reasoning model
            # that writes the tool call as prose inside `next_thought` and omits
            # the `next_tool_name`/`next_tool_args` sections entirely. That is a
            # single bad DRAW, not a systematic format: with cache=False at temp>0
            # an independent re-draw almost always emits the full sections. Re-issue
            # the whole call (re-format + re-sample + re-parse) up to N times, then
            # surface the error so the extract-repair / error path still owns it.
            # N is per-model (reasoning models only) -- see create_chat_adapter.
            attempts = self._clio_resample_attempts() + 1
            last_exc: Exception | None = None
            for i in range(attempts):
                try:
                    return super().__call__(lm, lm_kwargs, signature, demos, inputs)
                except AdapterParseError as exc:
                    last_exc = exc
                    if i + 1 < attempts:
                        self._clio_trace_resample(i, exc)
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            attempts = self._clio_resample_attempts() + 1
            last_exc: Exception | None = None
            for i in range(attempts):
                try:
                    return await super().acall(lm, lm_kwargs, signature, demos, inputs)
                except AdapterParseError as exc:
                    last_exc = exc
                    if i + 1 < attempts:
                        self._clio_trace_resample(i, exc)
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

    # DSPy's streaming support is gated by an allowlist keyed on the adapter's
    # CLASS NAME STRING (dspy/streaming/streaming_listener.py: it checks
    # ``settings.adapter.__class__.__name__ in {"ChatAdapter","XMLAdapter",
    # "JSONAdapter"}``, NOT isinstance). Our lenient subclass IS a ChatAdapter but
    # its name ("LenientChatAdapter") isn't in that list, so DSPy raises
    # "Unsupported adapter for streaming: LenientChatAdapter" the moment a content
    # chunk streams — which surfaced as nemotron/Sophia's TaskGroup/ExceptionGroup
    # "live streaming failed before emitting output". Report the name as
    # "ChatAdapter" so streaming is accepted; isinstance/behavior are unchanged.
    LenientChatAdapter.__name__ = "ChatAdapter"
    LenientChatAdapter.__qualname__ = "ChatAdapter"

    _LENIENT_CHAT_ADAPTER_CLS = LenientChatAdapter
    return _LENIENT_CHAT_ADAPTER_CLS


def _guided_output_enabled() -> bool:
    """Whether to use guided/structured output (dspy.JSONAdapter) instead of the
    text-protocol ChatAdapter.

    Guided output makes the provider CONSTRAIN generation to the signature's
    output schema (``response_format`` → json_schema when the signature allows,
    else json_object on LM Studio / vLLM), so the structured fields are valid by
    construction instead of relying on the model reproducing the
    ``[[ ## field ## ]]`` text format. This is the reasoning-model fix: qwopus
    drops fields (e.g. ReAct's ``next_tool_name``) under the text protocol; under
    guided output it emits schema-conformant JSON (which, on LM Studio, lands in
    ``reasoning_content`` and is recovered by the content←reasoning_content
    fallback in :meth:`IOLoggingLM._process_completion`).

    Configurable (``lm.guided_output`` / ``CLIO_LM_GUIDED_OUTPUT``), default OFF
    so models that pass on the text protocol (gpt-oss/gemma/nemotron) are
    untouched; opt in per grind / per model.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return bool(
            conf.resolve(
                "lm.guided_output",
                env="CLIO_LM_GUIDED_OUTPUT",
                default=False,
                cast=conf.as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break adapter construction
        from clio_agent import conf  # noqa: PLC0415

        try:
            return conf.as_bool(os.environ.get("CLIO_LM_GUIDED_OUTPUT", ""))
        except ValueError:
            return False


def _live_streaming_enabled() -> bool:
    """Whether the top-level GACT turn streams the agent's answer live
    (``dspy.streamify`` in :func:`gact.app._try_streamed_forward`) or runs the
    canonical BLOCKING path instead.

    Default ON — unchanged behavior for every model that streams cleanly
    (gpt-oss / gemma / qwopus). The escape hatch exists because some
    reasoning-model + provider combinations stream their answer entirely on the
    ``reasoning_content`` delta channel — which DSPy's content-only stream
    listeners cannot fold into the answer, and which bypasses the
    ``content←reasoning_content`` recovery in
    :meth:`IOLoggingLM._process_completion` (that recovery only runs on the
    blocking path). Symptoms (observed on nvidia/nemotron over ALCF Sophia):
    an empty answer (``stream_completed_without_chunks`` → ``empty_response``)
    or a streamify async ``ExceptionGroup`` ("live streaming failed before
    emitting output"). Disabling live streaming routes such a model through the
    blocking path, where the reasoning channel is recovered and there is no
    streamify task group to fail.

    Configurable (``runtime.live_streaming`` / ``CLIO_LIVE_STREAMING``), default
    ON; opt OUT per grind / per model.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return bool(
            conf.resolve(
                "runtime.live_streaming",
                env="CLIO_LIVE_STREAMING",
                default=True,
                cast=conf.as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break streaming; default on
        return True


def _reasoning_model_capability(config: LMProviderConfig) -> bool:
    """Per-model: is this a reasoning model (qwopus / qwen3-family ...)?

    Reasoning models route their real output into the ``reasoning_content``
    channel and, under the text protocol, intermittently drop a field on a single
    draw. Two reasoning-only behaviors hang off this flag — the
    ``content<-reasoning_content`` extraction (:meth:`IOLoggingLM._process_completion`)
    and the bounded parse re-sample (the lenient adapter) — so both are applied
    PER MODEL, not globally (today only qwopus/qwen match; others are untouched).

    Override with ``CLIO_LM_REASONING_MODEL`` (1/0); otherwise the per-model
    capability (the handshake ``is_reasoning`` flag, else the name-marker
    detection that reliably identifies qwopus/qwen) decides. This is the interim
    home for what tasks #33/#34 move into the model DB.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    # Tri-state: an explicit file/env value forces the flag; absence falls through
    # to the per-model capability detection below.
    raw = conf.resolve("lm.reasoning_model", env="CLIO_LM_REASONING_MODEL", default=None)
    if raw is not None:
        try:
            return conf.as_bool(raw)
        except ValueError:
            pass
    if bool(getattr(config, "is_reasoning", False)):
        return True
    return _uses_local_reasoning_model_profile(config.provider, config.model)


def _parse_retry_attempts(config: LMProviderConfig) -> int:
    """How many times to re-sample the LM on an unrecoverable adapter parse
    failure. Per-model: reasoning models (temp>0, independent re-draws) benefit;
    greedy/non-reasoning models would just repeat the same bad draw, so 0.
    Override with ``CLIO_LM_PARSE_RETRY_ATTEMPTS``."""
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    raw = conf.resolve(
        "limits.lm_parse_retry_attempts", env="CLIO_LM_PARSE_RETRY_ATTEMPTS", default=None
    )
    if raw is not None:
        try:
            return max(0, conf.as_int(raw))
        except (ValueError, TypeError):
            pass
    return 2 if _reasoning_model_capability(config) else 0


def _fix_guided_schema(part: Any) -> None:
    """In-place: pin declared object keys (``additionalProperties=false`` so a
    native-tool-calling model can't substitute its own ``{tool, arguments}``
    shape) while leaving open-ended objects (e.g. ReAct's ``next_tool_args``)
    permissive. Recurses into properties/items/$defs."""
    if not isinstance(part, dict):
        return
    if part.get("type") == "object":
        props = part.get("properties")
        if props:
            part["additionalProperties"] = False
            for sub in props.values():
                _fix_guided_schema(sub)
        else:
            part["additionalProperties"] = True
    if part.get("type") == "array" and isinstance(part.get("items"), dict):
        _fix_guided_schema(part["items"])
    for key in ("$defs", "definitions"):
        for sub in (part.get(key) or {}).values():
            _fix_guided_schema(sub)


def _signature_strict_response_format(signature: Any) -> dict[str, Any]:
    """Build a ``json_schema`` response_format that PINS a DSPy signature's output
    field NAMES (required-as-declared, no extra keys), so a reasoning model that
    natively emits ``{tool, arguments}`` (qwopus) is forced into the requested
    ``{next_thought, next_tool_name, next_tool_args}`` shape.

    Reuses DSPy's pydantic-based schema generation (handles Literal/list/nested),
    but replaces DSPy's open-ended guard+enforce_required (which raises on, or
    over-constrains, ``dict[str, Any]`` leaves) with :func:`_fix_guided_schema`.
    ``strict: false`` because open-ended leaves keep ``additionalProperties:true``
    (incompatible with OpenAI strict mode); LM Studio honors it (verified live).
    """
    import pydantic  # noqa: PLC0415

    fields: dict[str, Any] = {}
    for name, field_info in signature.output_fields.items():
        annotation = field_info.annotation
        default = field_info.default if hasattr(field_info, "default") else ...
        fields[name] = (annotation, default)
    model = pydantic.create_model(
        "ClioGuidedOutputs",
        __config__=pydantic.ConfigDict(extra="forbid"),
        **fields,
    )
    schema = model.model_json_schema()
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("json_schema_extra", None)
    _fix_guided_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "clio_output", "strict": False, "schema": schema},
    }


_STRICT_GUIDED_ADAPTER_CLS: Any = None


def _strict_guided_json_adapter_cls() -> Any:
    """Build (once) a JSONAdapter subclass that sends a field-name-pinned strict
    json_schema (see :func:`_signature_strict_response_format`) instead of DSPy's
    ``{"type":"json_object"}`` fallback.

    DSPy's JSONAdapter falls back to loose ``json_object`` for any signature with
    an open-ended field, and (a) LM Studio rejects that form, (b) loose lets the
    model emit its native ``{tool, arguments}`` keys -> 0 fields parsed. This
    subclass overrides __call__/acall to set our pinned schema and dispatch via
    ChatAdapter (which uses ``self.parse`` = JSONAdapter's JSON parse). On any
    schema-build failure it defers to stock JSONAdapter behavior.
    """
    global _STRICT_GUIDED_ADAPTER_CLS  # noqa: PLW0603
    if _STRICT_GUIDED_ADAPTER_CLS is not None:
        return _STRICT_GUIDED_ADAPTER_CLS
    dspy = _dspy()

    class StrictGuidedJSONAdapter(dspy.JSONAdapter):  # type: ignore[name-defined]
        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            if "response_format" in getattr(lm, "supported_params", []):
                try:
                    kwargs = {
                        **lm_kwargs,
                        "response_format": _signature_strict_response_format(signature),
                    }
                    return dspy.ChatAdapter.__call__(self, lm, kwargs, signature, demos, inputs)
                except Exception as exc:  # noqa: BLE001 - fall back to stock JSONAdapter
                    logger.warning(
                        "strict guided-JSON call failed; degrading to stock JSONAdapter "
                        "reason=strict_response_format_fallback signature=%s error=%s",
                        getattr(signature, "__name__", signature),
                        exc,
                    )
            return dspy.JSONAdapter.__call__(self, lm, lm_kwargs, signature, demos, inputs)

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            if "response_format" in getattr(lm, "supported_params", []):
                try:
                    kwargs = {
                        **lm_kwargs,
                        "response_format": _signature_strict_response_format(signature),
                    }
                    return await dspy.ChatAdapter.acall(self, lm, kwargs, signature, demos, inputs)
                except Exception as exc:  # noqa: BLE001 - fall back to stock JSONAdapter
                    logger.warning(
                        "strict guided-JSON call failed; degrading to stock JSONAdapter "
                        "reason=strict_response_format_fallback signature=%s error=%s",
                        getattr(signature, "__name__", signature),
                        exc,
                    )
            return await dspy.JSONAdapter.acall(self, lm, lm_kwargs, signature, demos, inputs)

    _STRICT_GUIDED_ADAPTER_CLS = StrictGuidedJSONAdapter
    return _STRICT_GUIDED_ADAPTER_CLS


def create_chat_adapter(config: LMProviderConfig) -> Any:
    """Create the DSPy adapter appropriate for this provider.

    Default: ChatAdapter's text protocol (local OpenAI-compatible servers work
    best with it) wrapped in a lenient subclass that, on a structured-output
    parse failure, coerces a constructor-repr field (e.g. qwopus emitting
    ``workflow_state`` as ``Model(field=...)`` instead of JSON) into JSON and
    re-parses — fixing the model↔adapter mismatch in code, no re-request.

    When guided output is enabled (:func:`_guided_output_enabled`), return
    ``dspy.JSONAdapter`` instead: it sends ``response_format`` so the provider
    constrains generation to the output schema — the durable fix for reasoning
    models that drop required fields under the text protocol. LM Studio honors
    ``response_format`` (verified live: it returns schema-conformant JSON, in
    ``reasoning_content``, recovered by the completion fallback); the historical
    "LM Studio rejects response_format with HTTP 400" note no longer holds.

    DSPy's JSON-adapter fallback is kept ONLY for remote providers. On a local
    backend it was historically harmful (the JSON-mode retry's ``response_format``
    once 400'd); local backends rely on the lenient coercion instead.
    ``CLIO_DISABLE_JSON_ADAPTER_FALLBACK`` force-disables it anywhere.
    """
    if _guided_output_enabled():
        return _strict_guided_json_adapter_cls()()
    use_json_fallback = not is_local_openai_compatible_backend(config)
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    if conf.resolve(
        "lm.disable_json_adapter_fallback",
        env="CLIO_DISABLE_JSON_ADAPTER_FALLBACK",
        default=False,
        cast=conf.as_bool,
    ):
        use_json_fallback = False
    adapter = _lenient_chat_adapter_cls()(use_json_adapter_fallback=use_json_fallback)
    # Per-model bounded re-sample on an unrecoverable parse failure (reasoning
    # models only; see _parse_retry_attempts). This is the base-case fix for a
    # reasoning model dropping a section (e.g. ReAct's next_tool_name) on one draw.
    adapter._clio_parse_retry = _parse_retry_attempts(config)
    return adapter


# ============================================================================
# LM STUDIO MODEL FETCHING
# ============================================================================


class LMStudioDiscoveryError(RuntimeError):
    """LM Studio model discovery failed before a usable chat model was found."""


def list_lm_studio_models(
    base_url: str = "http://127.0.0.1:1234", max_retries: int = 10, retry_delay: float = 2.0
) -> List[str]:
    """Discover loaded LM Studio model IDs through the unified provider handshake.

    This is the single LM Studio discovery path: the CLI (here) and the gact
    server both go through :class:`LMStudioHandshake`, so there is no longer a
    second, divergent probe to rot. The former standalone HTTP fetch is gone;
    its one genuinely-CLI-specific behaviour — *retry while LM Studio is still
    loading a model* — is preserved here: a reachable-but-empty result is
    retried up to ``max_retries`` times. A persistently unreachable backend, or
    one that never reports a loaded model, raises :class:`LMStudioDiscoveryError`
    with actionable text, exactly as before.

    Args:
        base_url: LM Studio base URL (with or without a ``/v1`` suffix).
        max_retries: Maximum probe attempts while waiting for a model to load.
        retry_delay: Delay between attempts in seconds.

    Returns:
        List of loaded model IDs.
    """
    import time

    from clio_agent.providers.handshake import HandshakeContext, run_handshake_sync

    last_error: str | None = None
    for attempt in range(max_retries):
        report = run_handshake_sync(
            HandshakeContext(
                provider_id="lm_studio",
                provider_kind="lm_studio",
                api_base=base_url,
                auth_mode="passive",
                # Names only: the context cascade (models.dev/db) isn't needed to
                # pick a model, and skipping it keeps discovery offline-fast.
                allow_external_sources=False,
            ),
            # Bypass the handshake TTL cache so each retry re-probes a backend
            # that may still be loading its first model.
            force=True,
        )
        if report.ok and report.models:
            return [m.id for m in report.models if m.id]
        last_error = report.error or "no loaded models reported"
        if attempt == 0:
            print(f"Connecting to LM Studio at {base_url}...")
        print(f"   Waiting for a loaded model... (attempt {attempt + 1}/{max_retries})")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    raise LMStudioDiscoveryError(
        f"LM Studio discovery failed at {base_url} after {max_retries} attempt(s): "
        f"{last_error}. Start LM Studio, load a chat/instruct model, or set "
        "CLIO_LM_API_BASE / CLIO_LM_MODEL."
    )


def _openai_compatible_api_base(base_url: str) -> str:
    """Return an OpenAI-compatible API base with exactly one /v1 suffix."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def select_models_for_agents(models: List[str]) -> tuple[str, str]:
    """Select main and expert models from available models.

    Prioritizes instruction-tuned/chat models and avoids embedding models.

    Args:
        models: List of available model IDs

    Returns:
        Tuple of (main_model, expert_model)

    Raises:
        ValueError: If discovery returned no usable chat/instruct model.
    """
    if not models:
        raise ValueError(
            "LM Studio reported no loaded models. Load a chat/instruct model, "
            "set CLIO_LM_MODEL explicitly, or reconfigure CLIO_LM_PROVIDER."
        )

    # Filter out embedding models
    chat_models = [m for m in models if "embedding" not in m.lower()]

    if not chat_models:
        raise ValueError(
            "LM Studio reported only embedding/non-chat models. Load a chat/instruct "
            f"model or set CLIO_LM_MODEL explicitly. Models: {', '.join(models)}"
        )

    # Strategy 1: Look for granite chat models
    granite_models = [m for m in chat_models if "granite" in m.lower()]

    if granite_models:
        main_model = granite_models[0]
        # Try to find a different granite model for expert, or use the same one
        if len(granite_models) > 1:
            expert_model = granite_models[1]
        else:
            expert_model = main_model
    else:
        # Strategy 2: If no granite models, take any available chat model.
        main_model = chat_models[0]
        # Try to pick a different model if possible
        remaining_models = [m for m in chat_models if m != main_model]
        if remaining_models:
            expert_model = remaining_models[0]
        else:
            expert_model = main_model

    print("Selected models:")
    print(f"  Main/Router: {main_model}")
    print(f"  Expert/Reasoner: {expert_model}")

    return main_model, expert_model


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

    except Exception as e:
        print(f"\nError: {e}")
        print("\nTroubleshooting:")
        print("- Ensure LM provider is running")
        print("- Check CLIO_LM_* environment variables")
