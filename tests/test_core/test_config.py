"""
Tests for clio_agent.config module.

The legacy LM-Studio-specific dataclasses (LMStudioConfig / RouterLMConfig
/ ReasonerLMConfig) and their configure_dspy_*_lm_studio factories were
removed alongside the provider registry refactor (umbrella iowarp/clio-
agent#48, sprint #50). The canonical surface is now
LMProviderConfig + create_lm() / create_planner_lm() driven by the
PROVIDER_DEFAULTS dict derived from clio_agent.providers.catalog.
"""

import pytest

from clio_agent.config import (
    _recover_malformed_structured_value,
    _unwrap_self_named_envelope,
    select_models_for_agents,
)


class TestStructuredValueRecovery:
    """Recovery of structured output fields from local-model malformations.

    These are format-only repairs (no semantic change) the lenient ChatAdapter
    applies when the strict parse fails -- the exact failure modes reasoning/
    small models produce on JSON-object fields.
    """

    def test_unwrap_self_named_envelope(self):
        """A value framed under its own field name is unwrapped."""
        wrapped = {"workflow_state": {"catalog": {"status": "metadata_found"}}}
        assert _unwrap_self_named_envelope(wrapped, "workflow_state") == {
            "catalog": {"status": "metadata_found"}
        }

    def test_unwrap_leaves_genuine_single_key_payload(self):
        """A single-key dict whose key is NOT the field name is left intact."""
        payload = {"catalog": {"status": "metadata_found"}}
        assert _unwrap_self_named_envelope(payload, "workflow_state") == payload

    def test_recover_double_wrapped_with_dropped_brace(self):
        """The exact qwopus failure: self-named envelope + a missing closing brace."""
        # 7 '{' vs 6 '}' -- json_repair rebalances, then the envelope is unwrapped.
        malformed = (
            '{"workflow_state": {"catalog": {"status": "metadata_found"}, '
            '"acquisition": {"metadata_path": "/tmp/es_clean.csv", '
            '"analysis_ready": false}}'
        )
        recovered = _recover_malformed_structured_value("workflow_state", malformed)
        assert set(recovered.keys()) >= {"catalog", "acquisition"}
        assert recovered["acquisition"]["metadata_path"] == "/tmp/es_clean.csv"

    def test_recover_plain_valid_json_unchanged(self):
        """A well-formed, non-wrapped value round-trips untouched."""
        good = '{"catalog": {"status": "no_candidates"}}'
        assert _recover_malformed_structured_value("workflow_state", good) == {
            "catalog": {"status": "no_candidates"}
        }

    def test_recover_constructor_repr(self):
        """A Python constructor-repr value still coerces (legacy qwopus shape)."""
        repr_text = "State(catalog=Cat(status='metadata_found'), ready=false)"
        recovered = _recover_malformed_structured_value("workflow_state", repr_text)
        assert recovered["catalog"]["status"] == "metadata_found"
        assert recovered["ready"] is False


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

    def test_select_empty_surfaces_configuration_error(self):
        """With no discovered models, do not guess a hardcoded fallback."""
        with pytest.raises(ValueError, match="reported no loaded models"):
            select_models_for_agents([])

    def test_select_embedding_only_surfaces_configuration_error(self):
        """Embedding-only models are not usable for chat/planner turns."""
        with pytest.raises(ValueError, match="only embedding/non-chat models"):
            select_models_for_agents(["text-embedding-nomic-embed-text-v1.5"])


def test_lenient_chat_adapter_is_streamable() -> None:
    """The lenient ChatAdapter subclass must pass DSPy's streaming allowlist.

    DSPy gates streaming on ``settings.adapter.__class__.__name__`` (a STRING) being
    in {ChatAdapter, XMLAdapter, JSONAdapter} — NOT isinstance. Our subclass IS a
    ChatAdapter but a non-allowlisted name made DSPy raise "Unsupported adapter for
    streaming: LenientChatAdapter", which surfaced as nemotron/Sophia's TaskGroup
    "live streaming failed before emitting output". Regression: it must report a
    name DSPy accepts while staying a ChatAdapter with recovery intact.
    """
    import dspy

    from clio_agent.config import _lenient_chat_adapter_cls

    cls = _lenient_chat_adapter_cls()
    inst = cls(use_json_adapter_fallback=False)
    assert inst.__class__.__name__ in {"ChatAdapter", "XMLAdapter", "JSONAdapter"}
    assert isinstance(inst, dspy.ChatAdapter)
