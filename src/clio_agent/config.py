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

Centralized configuration for LM Studio provider.
Supports local LLM provider for development.

Usage:
    >>> from clio_agent.config import setup_dspy
    >>> lm = setup_dspy()
"""

from dataclasses import dataclass
from typing import List, Optional

import dspy
import requests

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
        print("⚠️ No chat/instruct models found. Using available models as fallback.")
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

    print("✓ Selected models:")
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

    # Use openai/ prefix - LM Studio is OpenAI-compatible
    # LiteLLM's lm_studio/ provider has response parsing issues
    model_name = f"openai/{cfg.model}"

    lm = dspy.LM(
        model=model_name,
        api_base=f"{cfg.base_url}/v1",
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_type="chat"
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
        model_type="chat"
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
        model_type="chat"
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
            print("✓ LM Studio configured")
            print(f"  URL: {config.base_url}")
            print(f"  Model: {config.model}")
            print(f"  Temperature: {config.temperature}")
            print(f"  Max Tokens: {config.max_tokens}")

    except Exception as e:
        print(f"\n❌ Failed to configure LM Studio: {e}")
        print("\nTroubleshooting:")
        print("  • Ensure LM Studio is running")
        print(f"  • Check server is accessible at {LMStudioConfig().base_url}")
        print("  • Verify model is loaded in LM Studio")
        raise

    # Configure DSPy globally with ChatAdapter for ReAct compatibility
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter())

    return lm


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClioAgent Configuration Test")
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
