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
from typing import Optional
from dataclasses import dataclass


# ============================================================================
# CONFIGURATION CLASSES
# ============================================================================

@dataclass
class LMStudioConfig:
    """Configuration for local LM Studio.

    Default settings optimized for WSL2 → Windows host setup.
    """
    base_url: str = "http://100.127.255.172:1234"
    model: str = "openai/gpt-oss-20b"
    temperature: float = 1.0
    top_p: float = 1.0
    frequency_penalty: float = 1.1
    max_tokens: int = 8000
    api_key: str = "lm-studio"  # LM Studio doesn't validate, but LiteLLM requires non-empty




# ============================================================================
# DSPY SETUP FUNCTIONS
# ============================================================================

def configure_dspy_lm_studio(config: Optional[LMStudioConfig] = None) -> dspy.LM:
    """Configure DSPy to use LM Studio.

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
            print(f"✓ Using LM Studio")
            print(f"  URL: {config.base_url}/v1")
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
