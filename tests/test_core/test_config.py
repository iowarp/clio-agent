"""
Tests for clio_agent.config module.

The legacy LM-Studio-specific dataclasses (LMStudioConfig / RouterLMConfig
/ ReasonerLMConfig) and their configure_dspy_*_lm_studio factories were
removed alongside the provider registry refactor (umbrella iowarp/clio-
agent#48, sprint #50). The canonical surface is now
LMProviderConfig + create_lm() / create_planner_lm() driven by the
PROVIDER_DEFAULTS dict derived from clio_agent.providers.registry.
"""

from clio_agent.config import select_models_for_agents


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
