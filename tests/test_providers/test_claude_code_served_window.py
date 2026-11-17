"""Claude Code serves a model's extended window only to a ``[1m]`` selection."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.runtime import context_tokens
from clio_agent.providers.capabilities.dialects import claude_code


def test_a_plain_selection_is_served_the_standard_window() -> None:
    assert claude_code.served_context_window("claude-sonnet-5-5", 1_000_000) == 200_000
    assert claude_code.served_context_window("claude-haiku-5-5", 200_000) == 200_000
    assert claude_code.served_context_window("claude-x", 150_000) == 150_000
    assert claude_code.served_context_window("claude-sonnet-5-5", None) is None


def test_a_1m_selection_is_served_the_catalog_maximum() -> None:
    assert claude_code.served_context_window("claude-sonnet-5-5[1m]", 1_000_000) == 1_000_000


def test_the_loop_window_for_claude_code_without_a_handshake(monkeypatch) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.providers.working_context.working_context_for", lambda cfg: None
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.lookup_native_context",
        lambda model: 1_000_000 if model == "claude-sonnet-5-5" else None,
    )
    plain = SimpleNamespace(provider="claude_code", model="claude-sonnet-5-5")
    extended = SimpleNamespace(provider="claude_code", model="claude-sonnet-5-5[1m]")
    other = SimpleNamespace(provider="anthropic", model="claude-sonnet-5-5")
    assert context_tokens._resolve_expert_context_window(plain) == 200_000
    assert context_tokens._resolve_expert_context_window(extended) == 1_000_000
    assert context_tokens._resolve_expert_context_window(other) == 1_000_000
