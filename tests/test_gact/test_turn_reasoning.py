"""Per-message reasoning effort reaches the turn's LM (I).

``behavior.reasoning_effort`` was accepted and stored but never read: only the
global ``thinking_level`` reached the LM. These tests drive the REAL chain a turn
uses -- the turn-local agent overlay, ``_dynamic_agent_lm_config`` (spec ->
resolver), ``materialize`` and ``create_lm`` -- and assert the provider-correct
kwargs land on the constructed ``dspy.LM``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.agents.builders import _dynamic_agent_lm_config
from clio_agent.gact.message_contract import MessageBehavior, PostMessageRequest
from clio_agent.gact.turn_reasoning import apply_turn_reasoning, message_reasoning_effort
from clio_agent.gact.types import AgentDef, Message
from clio_agent.lm.factory import create_lm
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


def _message(effort: str | None) -> Message:
    behavior: dict[str, Any] = {"execution_mode": "execute", "confirmation_policy": "ask"}
    if effort is not None:
        behavior["reasoning_effort"] = effort
    return Message(
        id="msg_1",
        session_id="sess_1",
        role="user",
        created_at="2026-09-23T00:00:00+00:00",
        updated_at="2026-09-23T00:00:00+00:00",
        parts=[],
        metadata={"behavior": behavior},
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)

    def _handshake(ctx: Any, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id=ctx.provider_id,
            provider_kind=ctx.provider_kind,
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            models=(ModelProfile(id=ctx.target_model or "m"),),
        )

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _handshake)


def _lm_kwargs(base: LMProviderConfig, effort: str | None) -> dict[str, Any]:
    agent_def = apply_turn_reasoning(_message(effort), AgentDef(id="main", title="Main"))
    resolved = _dynamic_agent_lm_config(SimpleNamespace(_provider_config=base), agent_def)
    cfg = resolved.materialize(SimpleNamespace(resolve=lambda *_args: "test-credential"))
    return dict(create_lm(cfg).kwargs)


def test_codex_message_effort_overrides_the_global_level() -> None:
    base = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="low")
    assert _lm_kwargs(base, "high")["codex_reasoning_effort"] == "high"
    assert _lm_kwargs(base, "xhigh")["codex_reasoning_effort"] == "xhigh"
    # No per-message level: the configured level still governs.
    assert _lm_kwargs(base, None)["codex_reasoning_effort"] == "low"


def test_openai_kind_message_effort_sets_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    base = LMProviderConfig(provider="openai", model="gpt-5", api_key="sk-test")
    assert _lm_kwargs(base, "high")["reasoning_effort"] == "high"
    assert "reasoning_effort" not in _lm_kwargs(base, None)


def test_argonne_message_effort_sets_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARGONNE_TOKEN", "alcf-test")
    base = LMProviderConfig(
        provider="argonne",
        provider_id="argonne_metis",
        model="openai/gpt-oss-120b",
        api_key="alcf-test",
    )
    assert _lm_kwargs(base, "high")["reasoning_effort"] == "high"


def test_anthropic_message_effort_sets_a_thinking_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    base = LMProviderConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="sk-ant")
    assert _lm_kwargs(base, "high")["thinking"] == {"type": "enabled", "budget_tokens": 24576}
    assert "thinking" not in _lm_kwargs(base, "off")


def test_claude_code_message_effort_sets_the_sdk_thinking_config() -> None:
    base = LMProviderConfig(provider="claude_code", model="opus")
    assert _lm_kwargs(base, "medium")["claude_code_thinking"] == {
        "type": "enabled",
        "budget_tokens": 8192,
        "display": "summarized",
    }


def test_overlay_is_local_and_leaves_the_stored_agent_untouched() -> None:
    stored = AgentDef(id="main", title="Main", parameters={"temperature": 0.2})
    turn_local = apply_turn_reasoning(_message("high"), stored)
    assert turn_local.parameters == {"temperature": 0.2, "thinking_level": "high"}
    assert turn_local.metadata["turn_reasoning_source"] == "per_message"
    assert stored.parameters == {"temperature": 0.2}
    assert apply_turn_reasoning(_message(None), stored) is stored


def test_unset_effort_is_absent_not_a_fabricated_default() -> None:
    assert MessageBehavior().reasoning_effort is None
    assert "reasoning_effort" not in PostMessageRequest().behavior.model_dump(exclude_none=True)
    assert message_reasoning_effort(_message(None)) == ""


def test_claude_code_message_effort_sends_the_sdk_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claude_code model reporting CLI effort levels gets the real SDK effort."""
    monkeypatch.setattr(
        "clio_agent.providers.reasoning_levels._claude_code_row",
        lambda _model: {"supported_effort_levels": ["low", "medium", "high", "xhigh", "max"]},
    )
    base = LMProviderConfig(provider="claude_code", model="claude-fable-5-1")
    assert _lm_kwargs(base, "max")["claude_code_thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "effort": "max",
    }
    assert _lm_kwargs(base, "off")["claude_code_thinking"] == {"type": "disabled"}


def test_anthropic_adaptive_model_message_effort_sends_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    base = LMProviderConfig(provider="anthropic", model="claude-opus-4-7", api_key="sk-ant")
    assert _lm_kwargs(base, "max")["reasoning_effort"] == "max"


def test_codex_message_minimal_effort_is_sent() -> None:
    base = LMProviderConfig(provider="codex", model="gpt-5.5")
    assert _lm_kwargs(base, "minimal")["codex_reasoning_effort"] == "minimal"
