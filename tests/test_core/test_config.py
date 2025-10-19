"""
Tests for claudio.config module.

Tests LM configuration and provider setup.
"""

import pytest
from unittest.mock import patch, MagicMock
from claudio.config import (
    LMStudioConfig,
    OpenAIConfig,
    OllamaConfig,
    configure_dspy_lm_studio,
    configure_dspy_ollama,
)


class TestLMStudioConfig:
    """Test LM Studio configuration."""

    def test_default_config(self):
        """Test default LM Studio configuration values."""
        config = LMStudioConfig()

        assert config.base_url == "http://100.127.255.164:1234"
        assert config.model == "openai/gpt-oss-20b"
        assert config.temperature == 1.0
        assert config.max_tokens == 8000

    def test_custom_config(self):
        """Test custom LM Studio configuration."""
        config = LMStudioConfig(
            base_url="http://localhost:1234",
            model="custom-model",
            temperature=0.5
        )

        assert config.base_url == "http://localhost:1234"
        assert config.model == "custom-model"
        assert config.temperature == 0.5


class TestOllamaConfig:
    """Test Ollama configuration."""

    def test_default_config(self):
        """Test default Ollama configuration values."""
        config = OllamaConfig()

        assert config.base_url == "http://localhost:11434"
        assert config.model == "llama3.1:8b"
        assert config.temperature == 0.7


# TODO: Add tests for:
# - configure_dspy_lm_studio()
# - configure_dspy_openai()
# - configure_dspy_ollama()
# - setup_dspy() with different providers
# These require mocking dspy.LM and dspy.configure
