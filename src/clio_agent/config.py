#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
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

import os
from dataclasses import dataclass
from ipaddress import ip_address
from typing import List, Literal, Optional
from urllib.parse import urlparse

import dspy
import requests

# ============================================================================
# PROVIDER DEFAULTS
# ============================================================================

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "lm_studio": {
        "api_base": "http://127.0.0.1:1234/v1",
        "model": "ibm/granite-4-h-tiny",
        "api_key": "lm-studio",
    },
    "ollama": {
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "granite3.1-dense:8b",
        "api_key": "ollama",
    },
    "openai": {
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "",  # Must come from OPENAI_API_KEY env
    },
    "anthropic": {
        "api_base": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "api_key": "",  # Must come from ANTHROPIC_API_KEY env
    },
}

# Environment variable names for cloud provider API keys
_CLOUD_API_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


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

    provider: Literal["lm_studio", "ollama", "openai", "anthropic"] = "lm_studio"
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 1.0
    max_tokens: int = 32000
    router_temperature: float = 0.3
    environment: str = "dev"

    def __post_init__(self) -> None:
        """Fill empty fields from provider defaults."""
        defaults = PROVIDER_DEFAULTS.get(self.provider, PROVIDER_DEFAULTS["lm_studio"])
        if not self.api_base:
            self.api_base = defaults["api_base"]
        if not self.model:
            self.model = defaults["model"]
        if not self.api_key:
            # For cloud providers, try environment variable
            env_var = _CLOUD_API_KEY_ENV.get(self.provider)
            if env_var:
                self.api_key = os.environ.get(env_var, "")
            else:
                self.api_key = defaults["api_key"]


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

    return config


def create_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a dspy.LM instance from provider config.

    For openai/anthropic, uses the provider prefix (e.g., 'openai/gpt-4o-mini').
    For lm_studio/ollama, uses 'openai/{model}' with custom api_base.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance
    """
    if config.provider in ("openai", "anthropic"):
        model_name = f"{config.provider}/{config.model}"
    else:
        # lm_studio and ollama are OpenAI-compatible
        model_name = f"openai/{config.model}"

    return dspy.LM(
        model=model_name,
        api_base=config.api_base,
        api_key=config.api_key,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        model_type="chat",
    )


def create_router_lm(config: LMProviderConfig) -> dspy.LM:
    """Create a lower-temperature LM for deterministic routing.

    Uses config.router_temperature instead of config.temperature.

    Args:
        config: LM provider configuration

    Returns:
        Configured dspy.LM instance with lower temperature
    """
    if config.provider in ("openai", "anthropic"):
        model_name = f"{config.provider}/{config.model}"
    else:
        model_name = f"openai/{config.model}"

    return dspy.LM(
        model=model_name,
        api_base=config.api_base,
        api_key=config.api_key,
        temperature=config.router_temperature,
        max_tokens=config.max_tokens,
        model_type="chat",
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

    for attempt in range(max_retries):
        try:
            response = requests.get(f"{base_url}/v1/models", timeout=10)
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
    cfg = config or LMStudioConfig()

    # Use openai/ prefix - LM Studio is OpenAI-compatible
    model_name = f"openai/{cfg.model}"

    lm = dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
    )

    return lm


def configure_dspy_router_lm_studio(config: Optional[RouterLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for router (deterministic)."""
    cfg = config or RouterLMConfig()
    model_name = f"openai/{cfg.model}"

    return dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
    )


def configure_dspy_reasoner_lm_studio(config: Optional[ReasonerLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for reasoner (creative)."""
    cfg = config or ReasonerLMConfig()
    model_name = f"openai/{cfg.model}"

    return dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat",
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
    use_json_fallback = not _is_local_openai_compatible_backend(config)
    dspy.configure(
        lm=lm,
        adapter=dspy.ChatAdapter(use_json_adapter_fallback=use_json_fallback),
    )

    return lm


def _is_local_openai_compatible_backend(config: LMProviderConfig) -> bool:
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
        predictor = dspy.Predict("question -> answer")
        result = predictor(question="What is 2+2?")
        print(f"Answer: {result.answer}")

        print("\nConfiguration working!")

    except Exception as e:
        print(f"\nError: {e}")
        print("\nTroubleshooting:")
        print("- Ensure LM provider is running")
        print("- Check CLIO_LM_* environment variables")
