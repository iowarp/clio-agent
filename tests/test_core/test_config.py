"""
Tests for clio_agent.config module.

Tests LM Studio configuration classes and DSPy setup functions.
"""


import dspy

from clio_agent.config import (
    LMStudioConfig,
    ReasonerLMConfig,
    RouterLMConfig,
    configure_dspy_lm_studio,
    configure_dspy_reasoner_lm_studio,
    configure_dspy_router_lm_studio,
    select_models_for_agents,
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
        assert config.api_key == "lm-studio"

    def test_custom_config(self):
        """Test custom LM Studio configuration."""
        config = LMStudioConfig(
            base_url="http://localhost:1234",
            model="custom-model",
            temperature=0.5,
        )
        assert config.base_url == "http://localhost:1234"
        assert config.model == "custom-model"
        assert config.temperature == 0.5


class TestRouterLMConfig:
    """Test Router LM configuration."""

    def test_default_temperature(self):
        """Router should use low temperature for deterministic routing."""
        config = RouterLMConfig()
        assert config.temperature == 0.3

    def test_default_model(self):
        """Router should default to granite model."""
        config = RouterLMConfig()
        assert config.model == "ibm/granite-4-h-tiny"

    def test_custom_model(self):
        """Router config should accept custom model."""
        config = RouterLMConfig(model="custom/router-model")
        assert config.model == "custom/router-model"


class TestReasonerLMConfig:
    """Test Reasoner LM configuration."""

    def test_default_temperature(self):
        """Reasoner should use higher temperature for creativity."""
        config = ReasonerLMConfig()
        assert config.temperature == 1.0

    def test_default_model(self):
        """Reasoner should default to granite model."""
        config = ReasonerLMConfig()
        assert config.model == "ibm/granite-4-h-tiny"


class TestConfigureFunctions:
    """Test DSPy LM configuration functions."""

    def test_configure_lm_studio_returns_lm(self):
        """configure_dspy_lm_studio should return a dspy.LM instance."""
        config = LMStudioConfig()
        lm = configure_dspy_lm_studio(config)
        assert isinstance(lm, dspy.LM)

    def test_configure_lm_studio_default(self):
        """configure_dspy_lm_studio with no args uses defaults."""
        lm = configure_dspy_lm_studio()
        assert isinstance(lm, dspy.LM)

    def test_configure_router_lm(self):
        """configure_dspy_router_lm_studio should return a dspy.LM."""
        config = RouterLMConfig()
        lm = configure_dspy_router_lm_studio(config)
        assert isinstance(lm, dspy.LM)

    def test_configure_router_lm_default(self):
        """configure_dspy_router_lm_studio with no args uses defaults."""
        lm = configure_dspy_router_lm_studio()
        assert isinstance(lm, dspy.LM)

    def test_configure_reasoner_lm(self):
        """configure_dspy_reasoner_lm_studio should return a dspy.LM."""
        config = ReasonerLMConfig()
        lm = configure_dspy_reasoner_lm_studio(config)
        assert isinstance(lm, dspy.LM)

    def test_configure_reasoner_lm_default(self):
        """configure_dspy_reasoner_lm_studio with no args uses defaults."""
        lm = configure_dspy_reasoner_lm_studio()
        assert isinstance(lm, dspy.LM)


class TestSelectModels:
    """Test model selection logic."""

    def test_select_from_multiple_models(self):
        """Should select main and expert from available models."""
        models = ["model-a", "model-b", "model-c"]
        main, expert = select_models_for_agents(models)
        assert main in models
        assert expert in models

    def test_select_single_model(self):
        """With one model, both main and expert should use it."""
        models = ["only-model"]
        main, expert = select_models_for_agents(models)
        assert main == "only-model"
        assert expert == "only-model"

    def test_select_prefers_granite(self):
        """Should prefer granite models when available."""
        models = ["other-model", "granite-chat-v1"]
        main, expert = select_models_for_agents(models)
        assert "granite" in main.lower()

    def test_select_filters_embedding(self):
        """Should filter out embedding models."""
        models = ["text-embedding-model", "chat-model"]
        main, expert = select_models_for_agents(models)
        assert main == "chat-model"

    def test_select_empty_fallback(self):
        """With empty list, should fall back to default model."""
        models = []
        main, expert = select_models_for_agents(models)
        assert main is not None
        assert expert is not None
