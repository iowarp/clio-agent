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
    LMProviderConfig,
    _recover_malformed_structured_value,
    _unwrap_self_named_envelope,
    create_chat_adapter,
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


def test_lenient_chat_adapter_restores_escaped_section_boundary() -> None:
    """A format-only encoded separator still reaches DSPy's typed field parser."""

    import dspy

    from clio_agent.config import _lenient_chat_adapter_cls

    class Signature(dspy.Signature):
        next_thought: str = dspy.OutputField()
        tool_calls: str = dspy.OutputField()

    completion = (
        "[[ ## next_thought ## ]]\nCreate the plot."
        r"\n[[ ## tool_calls ## ]]\n"
        '[{"name":"plot_plot_timeseries"}]'
    )
    parsed = _lenient_chat_adapter_cls()(use_json_adapter_fallback=False).parse(
        Signature, completion
    )

    assert parsed == {
        "next_thought": "Create the plot.",
        "tool_calls": '[{"name":"plot_plot_timeseries"}]',
    }


def _guided_vllm_config(monkeypatch: pytest.MonkeyPatch) -> LMProviderConfig:
    """Return a guided-output vLLM config with the live-reported 8K window."""

    monkeypatch.setenv("CLIO_LM_GUIDED_OUTPUT", "1")
    config = LMProviderConfig(
        provider_id="vllm",
        model="ibm-granite/granite-4.2-30b",
        max_tokens=2048,
    )
    config.context_window = 8192
    config.chosen_context = 8192
    return config


def test_guided_vllm_drops_tool_choice_when_native_tools_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never send vLLM ``tool_choice`` unless the request also carries tools."""

    import dspy
    from dspy.adapters.types.tool import ToolCalls

    class ToolSignature(dspy.Signature):
        question: str = dspy.InputField()
        tools: list[dspy.Tool] = dspy.InputField()
        tool_calls: ToolCalls = dspy.OutputField()

    adapter = create_chat_adapter(_guided_vllm_config(monkeypatch))
    calls: list[dict[str, object]] = []

    class TextOnlyVllm:
        model = "hosted_vllm/ibm-granite/granite-4.2-30b"
        supported_params = ["response_format"]
        supports_response_schema = True
        supports_function_calling = False
        kwargs = {"max_tokens": 128}

        def __call__(self, *, messages: list[dict[str, object]], **kwargs: object) -> list[str]:
            calls.append({"messages": messages, **kwargs})
            return ['{"tool_calls":[]}']

    result = adapter(
        TextOnlyVllm(),
        {"tool_choice": {"type": "function", "function": {"name": "submit"}}},
        ToolSignature,
        [],
        {"question": "finish", "tools": []},
    )

    assert result[0]["tool_calls"].tool_calls == []
    assert calls
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]


@pytest.mark.parametrize("supports_response_format", [True, False])
def test_guided_vllm_caps_output_to_the_remaining_context_window(
    monkeypatch: pytest.MonkeyPatch,
    supports_response_format: bool,
) -> None:
    """The formatted prompt plus output budget must fit the discovered window."""

    import dspy

    class AnswerSignature(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    class CapturingLM:
        model = "hosted_vllm/ibm-granite/granite-4.2-30b"
        supported_params = ["response_format"] if supports_response_format else []
        supports_response_schema = True
        supports_function_calling = False
        kwargs = {"max_tokens": 2048}

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def __call__(self, *, messages: list[dict[str, object]], **kwargs: object) -> list[str]:
            self.calls.append({"messages": messages, **kwargs})
            return ['{"answer":"ready"}']

    adapter = create_chat_adapter(_guided_vllm_config(monkeypatch))
    lm = CapturingLM()
    result = adapter(
        lm,
        {"max_tokens": 2048},
        AnswerSignature,
        [],
        {"question": "context " * 2300},
    )

    assert result == [{"answer": "ready"}]
    assert lm.calls
    assert int(lm.calls[0]["max_tokens"]) < 2048
