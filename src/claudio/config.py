#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Configuration Module

Centralized configuration for LM Studio provider.
Supports local LLM provider for development.

Usage:
    >>> from claudio.config import setup_dspy
    >>> lm = setup_dspy()
"""

import dspy
import os
import requests
from typing import Optional, List
from dataclasses import dataclass


# ============================================================================
# LM STUDIO MODEL FETCHING
# ============================================================================

def fetch_lm_studio_models(base_url: str = "http://127.0.0.1:1234", max_retries: int = 10, retry_delay: float = 2.0) -> List[str]:
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
            models = [model['id'] for model in data['data']]
            if models:
                return models
            else:
                print(f"⏳ Waiting for models to load in LM Studio... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
        except requests.exceptions.ConnectionError:
            if attempt == 0:
                print(f"⏳ Connecting to LM Studio at {base_url}...")
            print(f"   Retry {attempt + 1}/{max_retries}... (waiting {retry_delay}s)")
            time.sleep(retry_delay)
        except Exception as e:
            print(f"Error fetching models: {e}")
            return []

    print(f"❌ Could not connect to LM Studio after {max_retries} attempts")
    print(f"   Please ensure LM Studio is running at {base_url}")
    print(f"   and a model is loaded")
    return []


def select_models_for_agents(models: List[str]) -> tuple[str, str]:
    """Select main and expert models from available models.

    Args:
        models: List of available model IDs

    Returns:
        Tuple of (main_model, expert_model)
    """
    main_model = None
    expert_model = None

    # Look for specific models by full ID
    for model in models:
        # Main model: prefer openai/gpt-oss-20b or gpt-oss-120b
        if ("openai/gpt-oss" in model or "gpt-oss" in model) and main_model is None:
            main_model = model
        # Expert model: prefer granite models
        if "granite" in model and expert_model is None:
            expert_model = model

    # Fallback: use first available model
    if main_model is None:
        main_model = models[0] if models else "openai/gpt-oss-20b"
    if expert_model is None:
        # Use different model if available, else same as main
        expert_model = models[1] if len(models) > 1 else main_model

    print(f"✓ Selected models:")
    print(f"  Main/Router: {main_model}")
    print(f"  Expert/Reasoner: {expert_model}")

    return main_model, expert_model


# ============================================================================
# CONFIGURATION CLASSES
# ============================================================================
# CONFIGURATION CLASSES
# ============================================================================

@dataclass
class LMStudioConfig:
    """Configuration for local LM Studio main agent.

    Model is dynamically selected from LM Studio API.
    Per OpenAI/Unsloth recommendations: Temperature 1.0 for optimal reasoning;
    max_tokens 32000 based on model card (128K context, but set to 32K for responses).
    """
    base_url: str = "http://127.0.0.1:1234"
    model: str = "openai/gpt-oss-20b"  # Default, overridden by fetch
    temperature: float = 1.0  # Recommended for gpt-oss reasoning
    top_p: float = 1.0  # Default for gpt-oss
    frequency_penalty: float = 1.1  # Default for gpt-oss
    max_tokens: int = 32000  # Based on model card (128K context, 32K for responses)
    api_key: str = "lm-studio"


@dataclass
class RouterLMConfig:
    """Configuration for router LM (deterministic for accurate routing)."""
    base_url: str = "http://127.0.0.1:1234"
    model: str = "openai/gpt-oss-20b"  # Default, overridden by fetch
    temperature: float = 0.3  # Low for deterministic routing
    top_p: float = 0.8
    frequency_penalty: float = 0.5
    max_tokens: int = 32000
    api_key: str = "lm-studio"


@dataclass
class ReasonerLMConfig:
    """Configuration for reasoner/expert LM (dynamically selected from LM Studio)."""
    base_url: str = "http://127.0.0.1:1234"
    model: str = "ibm/granite-4-h-tiny"  # Default, overridden by fetch
    temperature: float = 1.0  # High for creative reasoning
    top_p: float = 1.0
    frequency_penalty: float = 0.5
    max_tokens: int = 32000
    api_key: str = "lm-studio"




# ============================================================================
# DSPY SETUP FUNCTIONS
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

    # Ensure model name has proper prefix
    model_name = cfg.model
    if not model_name.startswith("openai/"):
        model_name = f"openai/{model_name}"

    lm = dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        frequency_penalty=cfg.frequency_penalty,
        model_type="chat",
        max_tokens=cfg.max_tokens,
        supports_response_format=False  # Disable for LM Studio compatibility
    )

    return lm


def configure_dspy_router_lm_studio(config: Optional[RouterLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for router (deterministic)."""
    cfg = config or RouterLMConfig()
    model_name = cfg.model if cfg.model.startswith("openai/") else f"openai/{cfg.model}"
    return dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        frequency_penalty=cfg.frequency_penalty,
        model_type="chat",
        max_tokens=cfg.max_tokens,
        supports_response_format=False
    )


def configure_dspy_reasoner_lm_studio(config: Optional[ReasonerLMConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio for reasoner (creative)."""
    cfg = config or ReasonerLMConfig()
    model_name = cfg.model if cfg.model.startswith("openai/") else f"openai/{cfg.model}"
    return dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        frequency_penalty=cfg.frequency_penalty,
        model_type="chat",
        max_tokens=cfg.max_tokens,
        supports_response_format=False
    )






def setup_dspy(
    model: Optional[str] = None,
    verbose: bool = True
) -> dspy.LM:
    """Setup DSPy with LM Studio provider.

    Args:
        model: Optional model override
        verbose: If True, print configuration info

    Returns:
        Configured DSPy LM instance

    Example:
        >>> # Use LM Studio with default model
        >>> lm = setup_dspy()

        >>> # Use LM Studio with custom model
        >>> lm = setup_dspy(model="mistral:7b")
    """
    # Configure LM Studio with error handling
    try:
        config = LMStudioConfig()
        if model:
            config.model = model
        lm = configure_dspy_lm_studio(config)

        if verbose:
            print(f"✓ Using LM Studio (Main Agent)")
            print(f"  URL: {config.base_url}/v1")
            print(f"  Model: {config.model}")
            print(f"  Temperature: {config.temperature} (tuned for consistent chat)")
            print(f"  Top-P: {config.top_p} (balanced creativity)")
            print(f"  Frequency Penalty: {config.frequency_penalty} (reduced repetition)")
            print(f"  Max Tokens: {config.max_tokens} (longer responses)")

    except Exception as e:
        print(f"\n❌ Failed to configure LM Studio: {e}")
        print("\nTroubleshooting:")
        print("  • Ensure LM Studio is running")
        print(f"  • Check server is accessible at {LMStudioConfig().base_url}")
        print("  • Verify model is loaded in LM Studio")
        raise

    # Configure DSPy globally
    dspy.configure(lm=lm)

    return lm


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO Configuration Test")
    print("=" * 60)

    try:
        # Test LM Studio configuration
        print("\n1. Testing LM Studio configuration...")
        lm = setup_dspy()

        # Simple test prediction
        print("\n2. Testing simple prediction...")
        predictor = dspy.Predict("question -> answer")
        result = predictor(question="What is 2+2?")
        print(f"Answer: {result.answer}")

        print("\n✅ Configuration working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nTroubleshooting:")
        print("- Ensure LM Studio is running at configured URL")
        print("- Check that model is loaded in LM Studio")
