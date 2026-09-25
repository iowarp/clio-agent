"""The blocking claude_code completion path rides the ONE session-pooled client (S2 B1).

Pre-S2 this exercised a SEPARATE thread-backed ``_SdkSessionPool`` keyed by
``(model, cwd, thinking)`` (``claude_code_sdk_pool.py``). That module is
DELETED: ``ClaudeCodeLLM.completion()`` now collects the SAME per-GACT-session
streaming transport ``astreaming()`` uses (:func:`clio_agent.providers
.claude_code_litellm._astream_sdk`) via :func:`clio_agent.providers
.claude_code_blocking._run_sdk`. These tests pin:

1. **One client path** — ``_run_sdk`` drives ``_astream_sdk``, so a real
   (fake-SDK) round trip through it produces the same ``(text, usage)`` shape
   the old standalone pool did, with the raw SDK usage dict intact (not the
   litellm-summed shape the streaming chunks carry).
2. **Per-LM transport** — transport is read purely from the per-LM
   ``optional_params`` carried on the resolved ``LMProviderConfig``; the
   process-global ``CLIO_CLAUDE_CODE_TRANSPORT`` env var is *not* consulted.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_blocking import _run_sdk
from clio_agent.providers.claude_code_litellm import ClaudeCodeLLM


@pytest.fixture(autouse=True)
def reset_provider() -> None:
    """Each test starts and ends with a clean LiteLLM provider map + pool."""
    from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests

    claude_code_litellm._reset_for_tests()
    _reset_sessions_for_tests()
    yield
    claude_code_litellm._reset_for_tests()
    _reset_sessions_for_tests()


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"constructed": 0}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAssistantMessage:
        def __init__(self) -> None:
            self.content = [FakeTextBlock("blocking answer")]
            self.usage = {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 3}
            self.stop_reason = "end_turn"

    class FakeResultMessage:
        usage = {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 3}
        stop_reason = "end_turn"
        result = "blocking answer"
        is_error = False

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            yield FakeAssistantMessage()
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = type("FakeStreamEvent", (), {})
    fake_sdk.TextBlock = FakeTextBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


def test_run_sdk_collects_the_streaming_transport_with_raw_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_sdk`` (the blocking path) rides ``_astream_sdk`` and returns the
    RAW SDK usage dict — not the litellm-summed ``prompt_tokens`` shape the
    streaming chunks carry — so ``build_model_response`` still sees the
    cache-read/cache-creation breakdown.

    SABOTAGE: have ``_run_sdk`` return the litellm-summed usage instead of the
    raw dict -> ``usage["cache_read_input_tokens"]`` disappears -> red.
    """
    _install_fake_sdk(monkeypatch)

    text, usage = _run_sdk(prompt="hello", model="haiku", timeout=5.0, cwd="/w")

    assert text == "blocking answer"
    assert usage == {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 3}


def test_completion_delegates_to_run_sdk_with_system_prompt_and_call_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ClaudeCodeLLM.completion`` calls ``_run_sdk`` with the split system
    prompt (B4) and a call_index, not the pre-S2 ``(prompt, model, timeout,
    cwd, thinking)``-only shape."""
    seen: dict[str, Any] = {}

    def fake_run_sdk(**kwargs: Any) -> tuple[str, dict[str, int]]:
        seen.update(kwargs)
        return "pooled", {"input_tokens": 1, "output_tokens": 2}

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_run_sdk)

    resp = ClaudeCodeLLM().completion(
        model="claude_code/cc-sonnet",
        messages=[
            {"role": "system", "content": "You are CLIO."},
            {"role": "user", "content": "hi"},
        ],
        api_base="",
        custom_prompt_dict={},
        model_response=MagicMock(),
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},
    )

    assert resp.choices[0].message.content == "pooled"
    assert seen["system_prompt"] == "You are CLIO."
    assert "call_index" in seen
    assert seen["model"] == "sonnet"


def test_transport_read_from_config_only_ignores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport comes only from optional_params; the env var is not consulted.

    With ``CLIO_CLAUDE_CODE_TRANSPORT=exec`` set but no override in
    ``optional_params``, the DEFAULT_TRANSPORT (``sdk``) applies — proving the
    process-global env never leaks into the per-LM path.
    """
    monkeypatch.setenv("CLIO_CLAUDE_CODE_TRANSPORT", "exec")

    def fake_sdk(*, prompt, native_blocks, model, timeout, cwd, thinking=None, **_kwargs):
        return "sdk path", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_sdk)

    resp = ClaudeCodeLLM().completion(
        model="claude_code/cc-sonnet",
        messages=[{"role": "user", "content": "hi"}],
        api_base="",
        custom_prompt_dict={},
        model_response=MagicMock(),
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},  # no transport override -> DEFAULT_TRANSPORT (sdk)
    )

    assert resp.choices[0].message.content == "sdk path"


def test_optional_params_transport_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-LM optional_params transport is authoritative over the (ignored) env.

    v0.8.0: with the "exec" transport deleted, an explicit removed transport in
    optional_params raises the typed error even when the env says "sdk" — the
    env is never consulted on the per-LM path (#818).
    """
    monkeypatch.setenv("CLIO_CLAUDE_CODE_TRANSPORT", "sdk")

    def fake_sdk(**_kwargs):
        raise AssertionError("sdk transport must not be selected from env")

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_sdk)

    from clio_agent.providers.claude_code_litellm import ClaudeCodeExecError

    with pytest.raises(ClaudeCodeExecError, match="removed in the v0.8.0 cleanup"):
        ClaudeCodeLLM().completion(
            model="claude_code/cc-sonnet",
            messages=[{"role": "user", "content": "hi"}],
            api_base="",
            custom_prompt_dict={},
            model_response=MagicMock(),
            print_verbose=None,
            encoding=None,
            api_key=None,
            logging_obj=None,
            optional_params={"claude_code_transport": "exec"},
        )
