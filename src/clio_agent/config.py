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
Supports LM Studio, Ollama, OpenAI, and Anthropic providers.

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

import requests

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
#   max_tokens (override): 4 096 for Sophia's default Llama 3.1-8B
#                         (32 768-token context window — the shared
#                         32 000 default would leave ~768 tokens for the
#                         router/expert prompts, which the gateway 400s
#                         on. Users can override with CLIO_LM_MAX_TOKENS
#                         for larger-context models.)
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
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
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
        router_temperature: Lower temperature for deterministic routing
        environment: Deployment environment (dev/staging/production)
    """

    provider: Literal[
        "lm_studio", "ollama", "openai", "anthropic", "argonne", "codex"
    ] = "lm_studio"
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 1.0
    # 0 is a sentinel "use the provider's max_tokens override (see
    # PROVIDER_DEFAULTS) if it has one, else 32000". Callers who
    # explicitly pass any non-zero value win.
    max_tokens: int = 0
    router_temperature: float = 0.3
    environment: str = "dev"
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

    def __post_init__(self) -> None:
        """Fill empty fields + capability flags from provider defaults."""
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
        # this provider" — argonne's Sophia gateway 400s on the
        # global default of 32000 because Llama 3.1-8B has only a
        # 32 768-token context window.
        if self.max_tokens == 0:
            self.max_tokens = int(defaults.get("max_tokens", 32000))
        # Capability flags. defaults dict wins — these aren't user-set
        # via env vars (they're wire-protocol facts about the provider),
        # so re-reading on every config load is safe.
        self.strip_openai_prefix = bool(defaults.get("strip_openai_prefix", True))


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
        CLIO_LM_PROVIDER: Provider name (lm_studio, ollama, openai, anthropic)
        CLIO_LM_API_BASE: Override API base URL
        CLIO_LM_MODEL: Override model identifier
        CLIO_LM_API_KEY: Override API key
        CLIO_LM_TEMPERATURE: Override temperature
        CLIO_LM_MAX_TOKENS: Override max tokens
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

    # Parse numeric env vars
    temperature_str = os.environ.get("CLIO_LM_TEMPERATURE", "")
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
    if max_tokens_str:
        kwargs["max_tokens"] = int(max_tokens_str)

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


def create_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a dspy.LM instance from provider config.

    For openai/anthropic, uses the provider prefix (e.g., 'openai/gpt-4o-mini').
    For lm_studio/ollama, uses 'openai/{model}' with custom api_base.
    For codex, uses 'codex/{model}' routed through the LiteLLM
    ``CustomLLM`` registered by ``providers.codex_litellm``.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance
    """
    dspy = _dspy()
    _ensure_provider_registered(config)
    model_name = _resolve_model_name(config)

    extras = _thinking_kwargs(config)
    return dspy.LM(
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

    Only the codex provider needs this today (it's a LiteLLM CustomLLM).
    The import is gated on the provider so installs without the codex
    binary don't pay the import cost.
    """
    if config.provider == "codex":
        from clio_agent.providers.codex_litellm import ensure_registered  # noqa: PLC0415

        ensure_registered()


def _resolve_model_name(config: LMProviderConfig) -> str:
    """Prefix the configured model id for litellm.

    - ``openai`` / ``anthropic``: native litellm prefix.
    - ``codex``: routes through the registered CustomLLM
      (``providers.codex_litellm``) under the ``codex/`` prefix.
    - everything else (lm_studio, ollama, argonne, …): treated as
      OpenAI-compatible by litellm, so we prefix with ``openai/``.

    The only id we rewrite is ``openai/<rest>`` — strip the leading
    ``openai/`` to avoid the ``openai/openai/claude-haiku-4-5`` shape
    DSPy/litellm rejects (commit a2bac1a). HuggingFace-style ids like
    ``ibm/granite-4-h-tiny`` or ``meta-llama/Meta-Llama-3.1-8B-Instruct``
    pass through intact — the earlier "split at first slash" was too
    eager and silently mangled them.
    """
    if config.provider in ("openai", "anthropic"):
        return f"{config.provider}/{config.model}"
    if config.provider == "codex":
        bare = config.model.removeprefix("codex/")
        return f"codex/{bare}"
    bare = config.model
    if bare.startswith("openai/"):
        bare = bare[len("openai/"):]
    return f"openai/{bare}"


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
    if config.provider in ("openai", "lm_studio", "ollama", "argonne", "codex"):
        if n < 2000:
            effort = "low"
        elif n < 8000:
            effort = "medium"
        else:
            effort = "high"
        return {"reasoning_effort": effort}
    return {}


def create_router_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a lower-temperature LM for deterministic routing.

    Uses config.router_temperature instead of config.temperature.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance with lower temperature
    """
    dspy = _dspy()
    _ensure_provider_registered(config)
    model_name = _resolve_model_name(config)

    return dspy.LM(
        model=model_name,
        api_base=config.api_base,
        api_key=config.api_key,
        temperature=config.router_temperature,
        max_tokens=config.max_tokens,
        model_type="chat",
        cache=False,  # see create_lm — same rationale
        **_thinking_kwargs(config),
    )


# ============================================================================
# LM STUDIO MODEL FETCHING
# ============================================================================


def fetch_lm_studio_models(
    base_url: str = "http://127.0.0.1:1234", max_retries: int = 10, retry_delay: float = 2.0
) -> List[str]:
    """Fetch available models from LM Studio API with retry logic.

    Args:
        base_url: LM Studio base URL
        max_retries: Maximum connection attempts
        retry_delay: Delay between retries in seconds

    Returns:
        List of model IDs
    """
    import time

    models_url = _lm_studio_models_url(base_url)

    for attempt in range(max_retries):
        try:
            response = requests.get(models_url, timeout=10)
            response.raise_for_status()
            data = response.json()
            models = [model["id"] for model in data["data"]]
            if models:
                return models
            else:
                print(
                    f"Waiting for models to load in LM Studio... (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(retry_delay)
        except requests.exceptions.ConnectionError:
            if attempt == 0:
                print(f"Connecting to LM Studio at {base_url}...")
            print(f"   Retry {attempt + 1}/{max_retries}... (waiting {retry_delay}s)")
            time.sleep(retry_delay)
        except Exception as e:
            print(f"Error fetching models: {e}")
            return []

    print(f"Could not connect to LM Studio after {max_retries} attempts")
    print(f"   Please ensure LM Studio is running at {base_url}")
    print("   and a model is loaded")
    return []


def _lm_studio_models_url(base_url: str) -> str:
    """Return the normalized LM Studio model-list endpoint."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/models"
    return f"{normalized}/v1/models"


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
    """
    main_model = None
    expert_model = None

    # Filter out embedding models
    chat_models = [m for m in models if "embedding" not in m.lower()]

    if not chat_models:
        print("No chat/instruct models found. Using available models as fallback.")
        chat_models = models

    # Strategy 1: Look for granite chat models
    granite_models = [m for m in chat_models if "granite" in m.lower()]

    if granite_models:
        main_model = granite_models[0]
        # Try to find a different granite model for expert, or use the same one
        if len(granite_models) > 1:
            expert_model = granite_models[1]
        else:
            expert_model = main_model

    # Strategy 2: If no granite models, take any available chat model
    if main_model is None and chat_models:
        main_model = chat_models[0]

    if expert_model is None:
        # Try to pick a different model if possible
        remaining_models = [m for m in chat_models if m != main_model]
        if remaining_models:
            expert_model = remaining_models[0]
        else:
            expert_model = main_model

    # Fallback default if absolutely nothing found (shouldn't happen if models list is not empty)
    if main_model is None:
        main_model = "ibm/granite-4-h-tiny"
    if expert_model is None:
        expert_model = "ibm/granite-4-h-tiny"

    print("Selected models:")
    print(f"  Main/Router: {main_model}")
    print(f"  Expert/Reasoner: {expert_model}")

    return main_model, expert_model


# ============================================================================
# BACKWARD-COMPATIBLE CONFIGURATION CLASSES
# ============================================================================


@dataclass
class LMStudioConfig:
    """Configuration for LM Studio provider.

    Default: IBM Granite model at http://127.0.0.1:1234
    """

    base_url: str = "http://127.0.0.1:1234"
    model: str = "ibm/granite-4-h-tiny"
    temperature: float = 1.0
    max_tokens: int = 32000
    api_key: str = "lm-studio"


@dataclass
class RouterLMConfig:
    """Configuration for router LM (deterministic for accurate routing)."""

    base_url: str = "http://127.0.0.1:1234"
    model: str = "ibm/granite-4-h-tiny"
    temperature: float = 0.3
    max_tokens: int = 32000
    api_key: str = "lm-studio"


@dataclass
class ReasonerLMConfig:
    """Configuration for reasoner/expert LM."""

    base_url: str = "http://127.0.0.1:1234"
    model: str = "ibm/granite-4-h-tiny"
    temperature: float = 1.0
    max_tokens: int = 32000
    api_key: str = "lm-studio"


# ============================================================================
# BACKWARD-COMPATIBLE DSPY SETUP FUNCTIONS
# ============================================================================


def configure_dspy_lm_studio(config: Optional[LMStudioConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for main agent.

    Args:
        config: LMStudioConfig instance. If None, uses defaults.

    Returns:
        Configured DSPy LM instance

    Example:
        >>> lm = configure_dspy_lm_studio()
        >>> # Or with custom config
        >>> custom_config = LMStudioConfig(base_url="http://100.127.255.172:1234")
        >>> lm = configure_dspy_lm_studio(custom_config)
    """
    dspy = _dspy()
    cfg = config or LMStudioConfig()

    # Use openai/ prefix - LM Studio is OpenAI-compatible
    model_name = f"openai/{cfg.model}"

    lm = dspy.LM(
        model=model_name,
        api_base=_openai_compatible_api_base(cfg.base_url),
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
        cache=False,
    )

    return lm


def configure_dspy_router_lm_studio(config: Optional[RouterLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for router (deterministic)."""
    dspy = _dspy()
    cfg = config or RouterLMConfig()
    model_name = f"openai/{cfg.model}"

    return dspy.LM(
        model=model_name,
        api_base=_openai_compatible_api_base(cfg.base_url),
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
        cache=False,
    )


def configure_dspy_reasoner_lm_studio(config: Optional[ReasonerLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for reasoner (creative)."""
    dspy = _dspy()
    cfg = config or ReasonerLMConfig()
    model_name = f"openai/{cfg.model}"

    return dspy.LM(
        model=model_name,
        api_base=_openai_compatible_api_base(cfg.base_url),
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
        cache=False,
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

    # Local OpenAI-compatible servers often reject LiteLLM's JSON mode fallback
    # (`response_format={"type": "json_object"}`). Keep them on text chat
    # formatting; cloud providers can still use DSPy's JSON fallback.
    use_json_fallback = not is_local_openai_compatible_backend(config)
    dspy = _dspy()
    dspy.configure(
        lm=lm,
        adapter=dspy.ChatAdapter(use_json_adapter_fallback=use_json_fallback),
    )

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
