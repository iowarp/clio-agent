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

import os
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Literal, Mapping, Optional
from urllib.parse import urlparse

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
    environment: str = "dev"
    codex_transport: Literal["exec", "sdk"] = "exec"
    claude_code_transport: Literal["exec"] = "exec"
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
            # Cloud providers source from a well-known env var. Argonne /
            # ALCF mints a fresh Globus bearer token on demand — kept lazy
            # so installs without globus-sdk don't need the dep.
            env_var = _CLOUD_API_KEY_ENV.get(self.provider)
            if env_var:
                self.api_key = os.environ.get(env_var, "")
            elif self.provider == "argonne":
                self.api_key = _resolve_argonne_api_key()
            else:
                self.api_key = defaults["api_key"]
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
        if self.claude_code_transport != "exec":
            raise ValueError(
                f"claude_code_transport must be 'exec' (got {self.claude_code_transport!r})"
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

    Two escape hatches before we touch globus-sdk:

    1. ``CLIO_ARGONNE_TOKEN`` — explicit override. Set by automation
       that already has a token (e.g. a parent agent that ran
       ``argonne_auth.get_access_token`` and exports the result).
    2. ``CLIO_LM_API_KEY`` — already handled in ``load_config_from_env``;
       only see ``__post_init__`` if the user really left it blank.

    Otherwise we go through ``providers.argonne_auth``. The import is
    deferred so ``globus-sdk`` is only required when this provider is
    actually selected. We swallow ``GlobusUnavailable`` and return ""
    so ``__post_init__`` doesn't crash on machines without the dep —
    the LM call itself will surface the missing-dep error with
    actionable text once the user issues a query.
    """
    # Accept either CLIO's own override OR the env var ALCF's own
    # ecosystem (alcf-agentics-workflow, list_active_models.sh) uses,
    # since users often already have one of those exported. CLIO_*
    # wins when both are set so deliberate overrides don't surprise.
    override = (
        os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
        or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
        or os.environ.get("access_token", "").strip()
    )
    if override:
        return override

    try:
        from clio_agent.providers.argonne_auth import (  # noqa: PLC0415
            GlobusUnavailable,
            get_access_token,
            tokens_exist,
        )
    except Exception:  # pragma: no cover - import-time error
        return ""

    # Don't trigger an interactive OAuth flow from inside the config
    # constructor — that path runs from /health, /doctor, and TUI
    # introspection where blocking on a browser would be hostile.
    # If there's no stored token, surface "" and let the upstream
    # validator emit the actionable "run authenticate" message.
    if not tokens_exist():
        return ""

    try:
        return get_access_token()
    except GlobusUnavailable:
        # Logged elsewhere; let downstream error message guide the user.
        return ""
    except Exception:
        # OAuth could not complete (network, refresh expired, etc).
        # Returning "" lets the LM call fail with a clean 401 rather
        # than masking it behind config-load tracebacks.
        return ""


def load_config_from_env() -> LMProviderConfig:
    """Load LM configuration from environment variables.

    Reads CLIO_* environment variables with fallback to provider defaults.

    Environment variables:
        CLIO_LM_PROVIDER: Provider name (lm_studio, ollama, openai, anthropic,
            argonne, codex, claude_code)
        CLIO_LM_API_BASE: Override API base URL
        CLIO_LM_MODEL: Override model identifier
        CLIO_LM_API_KEY: Override API key
        CLIO_LM_TEMPERATURE: Override reasoner/chat temperature
        CLIO_LM_PLANNER_TEMPERATURE: Override planner temperature
        CLIO_LM_PLANNER_MAX_TOKENS: Override planner token cap
        CLIO_LM_MAX_TOKENS: Override max tokens
        CLIO_CODEX_TRANSPORT: Codex transport mode (exec or sdk)
        CLIO_CLAUDE_CODE_TRANSPORT: Claude Code transport mode (exec)
        CLIO_ENVIRONMENT: Deployment environment (dev/staging/production)

    Returns:
        LMProviderConfig with env-based settings

    Raises:
        ValueError: If cloud provider is selected without API key
    """
    provider = os.environ.get("CLIO_LM_PROVIDER", "lm_studio")
    api_base = os.environ.get("CLIO_LM_API_BASE", "")
    model = os.environ.get("CLIO_LM_MODEL", "")
    api_key = os.environ.get("CLIO_LM_API_KEY", "")
    environment = os.environ.get("CLIO_ENVIRONMENT", "dev")
    codex_transport = os.environ.get("CLIO_CODEX_TRANSPORT", "").strip().lower()
    claude_code_transport = os.environ.get("CLIO_CLAUDE_CODE_TRANSPORT", "").strip().lower()

    # Parse numeric env vars
    temperature_str = os.environ.get("CLIO_LM_TEMPERATURE", "")
    planner_temperature_str = os.environ.get(
        "CLIO_LM_PLANNER_TEMPERATURE",
        os.environ.get("CLIO_LM_ROUTER_TEMPERATURE", ""),
    )
    planner_max_tokens_str = os.environ.get("CLIO_LM_PLANNER_MAX_TOKENS", "")
    max_tokens_str = os.environ.get("CLIO_LM_MAX_TOKENS", "")

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
    if temperature_str:
        kwargs["temperature"] = float(temperature_str)
    if planner_temperature_str:
        kwargs["planner_temperature"] = float(planner_temperature_str)
    if planner_max_tokens_str:
        kwargs["planner_max_tokens"] = int(planner_max_tokens_str)
    if max_tokens_str:
        kwargs["max_tokens"] = int(max_tokens_str)
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
    """Return whether CLIO_LM_MODEL was explicitly set."""
    current_env = env or os.environ
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
    """
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
                import anyio as _anyio  # noqa: PLC0415

                from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                    note_lm_activity,
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

                async with _anyio.create_task_group() as tg:
                    tg.start_soon(_produce)
                    async with recv:
                        async for _chunk in recv:
                            note_lm_activity()
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
                from clio_agent.gact.app import (  # noqa: PLC0415
                    _ACTIVE_GACT_APP,
                    _ACTIVE_GACT_SESSION_ID,
                    _ACTIVE_GACT_TRACE_ID,
                    _ACTIVE_GACT_TURN_ID,
                    _emit_semantic_event,
                )
            except Exception:  # noqa: BLE001 - app may be unavailable (CLI/optimizer paths)
                return None
            app = _ACTIVE_GACT_APP.get()
            sid = _ACTIVE_GACT_SESSION_ID.get()
            if app is None or not sid:
                return None
            return (
                app,
                sid,
                _ACTIVE_GACT_TURN_ID.get(),
                _ACTIVE_GACT_TRACE_ID.get(),
                _emit_semantic_event,
            )

        def _clio_log_last_call(self) -> None:
            try:
                target = self._clio_trace_target()
                # No active GACT turn -> nothing to emit (CLI/optimizer paths).
                if target is None:
                    return
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
                # Emit the canonical trace's DURABLE-ONLY lm.call event: the one
                # place an expert call's raw messages + reasoning_content are
                # reliably visible (expert LMs run in executors the settle path
                # can't reach), captured on the failure path too. detail_level="off"
                # keeps it off SSE/UI. (Legacy CLIO_LOG_LM_IO JSONL mirror removed --
                # the canonical trace is the single recorder.)
                if target is not None:
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
                    except Exception:  # noqa: BLE001 - capture must never fail a call
                        pass
            except Exception:  # noqa: BLE001 - logging is best-effort, never fail a call
                pass

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
    return _construct_lm(
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


def create_router_lm(config: LMProviderConfig) -> dspy.LM:
    """Backward-compatible alias for create_planner_lm."""
    return create_planner_lm(config)


def _resolve_lm_studio_model_if_needed(config: LMProviderConfig) -> None:
    """Fill a blank LM Studio model from the currently loaded model list."""
    if config.provider == "lm_studio" and not config.model.strip():
        models = list_lm_studio_models(base_url=config.api_base)
        config.model, _ = select_models_for_agents(models)


def _provider_lm_kwargs(config: LMProviderConfig) -> dict[str, Any]:
    """Return provider-specific LiteLLM kwargs for dspy.LM construction."""
    extras = _thinking_kwargs(config)
    # Qwen-family reasoning models (e.g. qwopus) run their reasoning_content away
    # on the pipeline's structured routing/tool-decision calls — consuming the whole
    # token budget without reaching the decision (uncapped → >900s → wedge; capped →
    # no tool call). Structured routing does not need chain-of-thought, so disable
    # thinking when CLIO_LM_DISABLE_THINKING is set. enable_thinking=false is honored
    # by Qwen chat templates (verified: 8327 reasoning chars/43s → 0 chars/0.8s).
    if os.environ.get("CLIO_LM_DISABLE_THINKING", "").strip().lower() in {"1", "true", "yes"}:
        body = dict(extras.get("extra_body") or {})
        body["chat_template_kwargs"] = {
            **body.get("chat_template_kwargs", {}),
            "enable_thinking": False,
        }
        extras["extra_body"] = body
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
    path = os.environ.get("CLIO_DUMP_UNPARSEABLE", "").strip()
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
    except Exception:  # noqa: BLE001 - diagnostic only, never fail a turn
        pass


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


def create_chat_adapter(config: LMProviderConfig) -> Any:
    """Create the DSPy chat adapter appropriate for this provider.

    Uses ChatAdapter's text protocol (local OpenAI-compatible servers work best
    with it) wrapped in a lenient subclass that, on a structured-output parse
    failure, coerces a constructor-repr field (e.g. qwopus emitting
    ``workflow_state`` as ``Model(field=...)`` instead of JSON) into JSON and
    re-parses — fixing the model↔adapter mismatch in code, no re-request.

    DSPy's JSON-adapter fallback is kept ONLY for remote providers. On a local
    backend it is harmful: when it engages it retries with the JSON adapter, which
    sends ``response_format``, and LM Studio rejects that with HTTP 400 — turning a
    recoverable parse into a hard error. Local backends rely on the lenient
    coercion instead (verified: it recovers qwopus's constructor-repr without any
    re-request). ``CLIO_DISABLE_JSON_ADAPTER_FALLBACK`` force-disables it anywhere.
    """
    use_json_fallback = not is_local_openai_compatible_backend(config)
    if os.environ.get("CLIO_DISABLE_JSON_ADAPTER_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        use_json_fallback = False
    return _lenient_chat_adapter_cls()(use_json_adapter_fallback=use_json_fallback)


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
