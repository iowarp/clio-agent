#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
Warpio DSPy POC - Configuration

Centralized configuration for LM Studio and DSPy settings.
"""

import dspy
import os
from typing import Optional


# ============================================================================
# LM STUDIO CONFIGURATION
# ============================================================================

class LMStudioConfig:
    """Configuration for local LM Studio."""

    # Server settings (WSL2 to Windows host)
    BASE_URL = "http://100.127.255.164:1234"
    MODEL = "openai/gpt-oss-20b"

    # Generation parameters
    TEMPERATURE = 1.0
    TOP_K = 20
    TOP_P = 1.0
    FREQUENCY_PENALTY = 1.1

    # API key (LM Studio doesn't validate but LiteLLM requires non-empty)
    API_KEY = "lm-studio"



# ============================================================================
# DSPY SETUP
# ============================================================================

def configure_dspy_lm_studio() -> dspy.LM:
    """Configure DSPy to use LM Studio.

    Returns:
        Configured DSPy LM instance
    """
    # LM Studio is OpenAI-compatible but has limited support for some params
    # Use the full model path from LM Studio
    model_name = LMStudioConfig.MODEL
    if not model_name.startswith("openai/"):
        model_name = f"openai/{model_name}"

    lm = dspy.LM(
        model=model_name,
        api_base=LMStudioConfig.BASE_URL + "/v1",
        api_key=LMStudioConfig.API_KEY,
        temperature=LMStudioConfig.TEMPERATURE,
        top_p=LMStudioConfig.TOP_P,
        frequency_penalty=LMStudioConfig.FREQUENCY_PENALTY,
        model_type="chat",
        max_tokens=8000,
        # Disable structured outputs for LM Studio compatibility
        supports_response_format=False
    )

    return lm


def configure_dspy_openai() -> dspy.LM:
    """Configure DSPy to use OpenAI (fallback).

    Returns:
        Configured DSPy LM instance

    Raises:
        ValueError if OPENAI_API_KEY not set
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    lm = dspy.LM(
        model='openai/gpt-4o-mini',
        temperature=0.7
    )

    return lm


def setup_dspy(use_lm_studio: bool = True) -> dspy.LM:
    """Setup DSPy with appropriate LM.

    Args:
        use_lm_studio: If True, use LM Studio. If False, use OpenAI.

    Returns:
        Configured DSPy LM instance
    """
    if use_lm_studio:
        lm = configure_dspy_lm_studio()
        print(f"✓ Using LM Studio at {LMStudioConfig.BASE_URL}")
        print(f"  Model: {LMStudioConfig.MODEL}")
        print(f"  Temperature: {LMStudioConfig.TEMPERATURE}")
        print(f"  Top-P: {LMStudioConfig.TOP_P}")
        print(f"  Frequency Penalty: {LMStudioConfig.FREQUENCY_PENALTY}")
        print(f"  Max Tokens: 2000")
    else:
        lm = configure_dspy_openai()
        print("✓ Using OpenAI GPT-4o-mini")

    # Configure DSPy globally
    dspy.configure(lm=lm)

    return lm


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("Warpio DSPy POC - Configuration Test")
    print("=" * 50)

    try:
        # Test LM Studio configuration
        print("\n1. Testing LM Studio configuration...")
        lm = setup_dspy(use_lm_studio=True)

        # Simple test
        print("\n2. Testing simple prediction...")
        predictor = dspy.Predict("question -> answer")
        result = predictor(question="What is 2+2?")
        print(f"Answer: {result.answer}")

        print("\n✅ LM Studio configuration working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nMake sure LM Studio is running at http://127.0.0.1:1234")
        print("and the model 'openai/gpt-oss-20b' is loaded.")
