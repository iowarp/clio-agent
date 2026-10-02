"""
Tests for clio_agent.config module.

The legacy LM-Studio-specific dataclasses (LMStudioConfig / RouterLMConfig
/ ReasonerLMConfig) and their configure_dspy_*_lm_studio factories were
removed alongside the provider registry refactor (umbrella iowarp/clio-
agent#48, sprint #50). The canonical surface is now
LMProviderConfig + create_lm() driven by the
PROVIDER_DEFAULTS dict derived from clio_agent.providers.catalog.
"""

import pytest

from clio_agent.config import (
    LMProviderConfig,
    create_chat_adapter,
    select_models_for_agents,
)


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


# ---- #1326: _ContextOverflowError pre-flight for the non-guided path ----


def test_context_overflow_error_raised_when_prompt_exceeds_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A _ContextOverflowError fires before the LM call when prompt > window."""
    import dspy

    from clio_agent.lm.adapters import _ContextOverflowError

    class AnswerSig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    # Build a non-guided config with a tiny context window.
    config = LMProviderConfig(provider_id="vllm", model="ibm/granite-4.2-30b", max_tokens=0)
    config.context_window = 512
    config.chosen_context = 512

    adapter = create_chat_adapter(config)
    # Verify the window was stamped onto the adapter.
    assert int(getattr(adapter, "_clio_context_window", 0)) == 512

    class NeverCalledLM:
        model = "hosted_vllm/ibm/granite-4.2-30b"
        supported_params: list[str] = []
        kwargs: dict = {}

        def __call__(self, **kwargs: object) -> list[str]:
            raise AssertionError("LM should not be called when prompt overflows")

    with pytest.raises(_ContextOverflowError, match="tokens > context"):
        adapter(
            NeverCalledLM(),
            {},
            AnswerSig,
            [],
            {"question": "word " * 600},  # ~600+ tokens, well above the 512 window
        )


def test_context_overflow_error_no_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small prompt under the window should NOT raise _ContextOverflowError."""
    import dspy

    from clio_agent.lm.adapters import _ContextOverflowError

    class TinySig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    config = LMProviderConfig(provider_id="vllm", model="ibm/granite-4.2-30b", max_tokens=0)
    config.context_window = 32768
    config.chosen_context = 32768

    adapter = create_chat_adapter(config)
    responses: list[str] = []

    class EchoLM:
        model = "hosted_vllm/ibm/granite-4.2-30b"
        supported_params: list[str] = []
        kwargs: dict = {}

        def __call__(self, *, messages: list, **kwargs: object) -> list[str]:
            responses.append("ok")
            return ["[[ ## answer ## ]]\nok"]

    # Should not raise; a tiny prompt fits comfortably.
    try:
        adapter(EchoLM(), {}, TinySig, [], {"question": "hello"})
    except _ContextOverflowError:
        pytest.fail("_ContextOverflowError raised for a prompt well within the window")


def test_default_adapter_never_repairs_or_falls_back() -> None:
    """The default adapter is DSPy's ChatAdapter: no JSON fallback, no repair layer."""
    import dspy

    adapter = create_chat_adapter(LMProviderConfig(provider="openai", model="gpt-5", api_key="k"))
    assert isinstance(adapter, dspy.ChatAdapter)
    assert adapter.use_json_adapter_fallback is False
    assert type(adapter).__name__ == "ClioChatAdapter"
