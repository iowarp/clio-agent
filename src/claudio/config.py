#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Configuration Module

Centralized configuration for LM providers (LM Studio, Ollama, OpenAI).
Supports both local and cloud LLM providers with automatic fallback.

Usage:
    # Local LM Studio (primary for development)
    >>> from claudio.config import setup_dspy
    >>> lm = setup_dspy(use_lm_studio=True)

    # Cloud OpenAI (fallback or optimization)
    >>> lm = setup_dspy(use_openai=True)

    # Local Ollama
    >>> lm = setup_dspy(use_ollama=True, model="llama3.1:8b")
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
    base_url: str = "http://100.127.255.164:1234"
    model: str = "openai/gpt-oss-20b"
    temperature: float = 1.0
    top_p: float = 1.0
    frequency_penalty: float = 1.1
    max_tokens: int = 8000
    api_key: str = "lm-studio"  # LM Studio doesn't validate, but LiteLLM requires non-empty


@dataclass
class OpenAIConfig:
    """Configuration for OpenAI API.

    Requires OPENAI_API_KEY environment variable.
    """
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 4000


@dataclass
class OllamaConfig:
    """Configuration for Ollama local models.

    Default settings for local Ollama deployment.
    """
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    temperature: float = 0.7
    max_tokens: int = 4000


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
        >>> custom_config = LMStudioConfig(base_url="http://localhost:1234")
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


def configure_dspy_openai(config: Optional[OpenAIConfig] = None) -> dspy.LM:
    """Configure DSPy to use OpenAI.

    Args:
        config: OpenAIConfig instance. If None, uses defaults.

    Returns:
        Configured DSPy LM instance

    Raises:
        ValueError: If OPENAI_API_KEY environment variable not set

    Example:
        >>> import os
        >>> os.environ["OPENAI_API_KEY"] = "sk-..."
        >>> lm = configure_dspy_openai()
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY environment variable not set. "
            "Set it with: export OPENAI_API_KEY=sk-..."
        )

    cfg = config or OpenAIConfig()

    lm = dspy.LM(
        model=f"openai/{cfg.model}",
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens
    )

    return lm


def configure_dspy_ollama(config: Optional[OllamaConfig] = None) -> dspy.LM:
    """Configure DSPy to use Ollama.

    Args:
        config: OllamaConfig instance. If None, uses defaults.

    Returns:
        Configured DSPy LM instance

    Example:
        >>> lm = configure_dspy_ollama()
        >>> # Or with custom model
        >>> cfg = OllamaConfig(model="mistral:7b")
        >>> lm = configure_dspy_ollama(cfg)
    """
    cfg = config or OllamaConfig()

    lm = dspy.LM(
        model=f"ollama_chat/{cfg.model}",
        api_base=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens
    )

    return lm


def setup_dspy(
    use_lm_studio: bool = True,  # Default to LM Studio
    use_openai: bool = False,
    use_ollama: bool = False,
    model: Optional[str] = None,
    verbose: bool = True
) -> dspy.LM:
    """Setup DSPy with appropriate LM provider.

    Automatically selects and configures the LM provider. Priority:
    1. Explicitly enabled provider (lm_studio/openai/ollama)
    2. Falls back to LM Studio by default
    3. Auto-detects based on environment variables

    Args:
        use_lm_studio: If True, use LM Studio (DEFAULT)
        use_openai: If True, use OpenAI
        use_ollama: If True, use Ollama
        model: Optional model override
        verbose: If True, print configuration info

    Returns:
        Configured DSPy LM instance

    Example:
        >>> # Use LM Studio (default)
        >>> lm = setup_dspy()

        >>> # Use OpenAI
        >>> lm = setup_dspy(use_openai=True, use_lm_studio=False)

        >>> # Use Ollama with custom model
        >>> lm = setup_dspy(use_ollama=True, use_lm_studio=False, model="mistral:7b")
    """
    # Count explicitly requested providers
    providers_requested = sum([use_lm_studio, use_openai, use_ollama])

    # Ensure only one provider
    if providers_requested > 1:
        raise ValueError(
            "Only one LM provider can be enabled at a time. "
            f"Requested: lm_studio={use_lm_studio}, openai={use_openai}, ollama={use_ollama}"
        )

    # Configure selected provider with error handling
    try:
        if use_lm_studio:
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

        elif use_openai:
            config = OpenAIConfig()
            if model:
                config.model = model
            lm = configure_dspy_openai(config)

            if verbose:
                print(f"✓ Using OpenAI")
                print(f"  Model: {config.model}")
                print(f"  Temperature: {config.temperature}")

        elif use_ollama:
            config = OllamaConfig()
            if model:
                config.model = model
            lm = configure_dspy_ollama(config)

            if verbose:
                print(f"✓ Using Ollama")
                print(f"  URL: {config.base_url}")
                print(f"  Model: {config.model}")
                print(f"  Temperature: {config.temperature}")

    except Exception as e:
        print(f"\n❌ Failed to configure LM provider: {e}")
        print("\nTroubleshooting:")
        if use_lm_studio:
            print("  • Ensure LM Studio is running")
            print(f"  • Check server is accessible at {LMStudioConfig().base_url}")
            print("  • Verify model is loaded in LM Studio")
        elif use_openai:
            print("  • Set OPENAI_API_KEY environment variable")
            print("  • export OPENAI_API_KEY=sk-...")
        elif use_ollama:
            print("  • Ensure Ollama is running: ollama serve")
            print("  • Pull model: ollama pull llama3.1:8b")
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
        lm = setup_dspy(use_lm_studio=True)

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
        print("- For OpenAI: export OPENAI_API_KEY=sk-...")
        print("- For Ollama: ensure ollama service is running")
