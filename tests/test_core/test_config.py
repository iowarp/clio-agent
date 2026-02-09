"""
Tests for clio_agent.config module.

Tests LM Studio configuration and provider setup.
"""

import pytest
from unittest.mock import patch, MagicMock
from clio_agent.config import (
    LMStudioConfig,
    configure_dspy_lm_studio,
)


class TestLMStudioConfig:
    """Test LM Studio configuration."""

    def test_default_config(self):
        """Test default LM Studio configuration values."""
        config = LMStudioConfig()
    
        assert config.base_url == "http://127.0.0.1:1234"
        assert config.model == "ibm/granite-4-h-tiny"
        assert config.temperature == 1.0
        assert config.max_tokens == 32000

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
