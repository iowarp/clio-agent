"""
Tests for claudio.config module.

Tests LM Studio configuration and provider setup.
"""

import pytest
from unittest.mock import patch, MagicMock
from claudio.config import (
    LMStudioConfig,
    configure_dspy_lm_studio,
)


class TestLMStudioConfig:
    """Test LM Studio configuration."""

    def test_default_config(self):
        """Test default LM Studio configuration values."""
        config = LMStudioConfig()

        assert config.base_url == "http://100.127.255.172:1234"
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


# TODO: Add tests for:
# - configure_dspy_lm_studio()
# - setup_dspy()
# These require mocking dspy.LM and dspy.configure
