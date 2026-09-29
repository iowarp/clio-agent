"""Transport-level wiring of the provider-generic thinking knob (#895).

Proves the level travels config → factory kwargs → the Claude Code engine → the SDK
client: the LM-factory mapping, the factory handing the thinking config to the
engine, the engine reaching ``ClaudeAgentOptions.thinking``, and (S2 B13) the
session pool's own-entry reconnect on a thinking-config change. The env/config
plumbing and typed-unsupported surfacing round it out.
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig, load_config_from_env
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers import claude_code_engine
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.records import (
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
)
from clio_agent.providers.claude_code_options import build_sdk_options, thinking_key
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests.env_isolation import isolated_environ

_NOW = "2026-09-25T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_capability_state():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


# --------------------------------------------------------------------------- #
# LM-factory mapping: config.thinking_level -> provider-specific kwargs.
#
# claude_code/anthropic/codex/openai now go through the SAME dialect_wire
# mapping every other dialect does (lm.dialect_wire.thinking_wire), driven by
# each model's own real ThinkingSpec (providers.capabilities.dialects.codex /
# .claude_code / .cloud_thinking) -- see tests/test_providers/
# test_request_builder.py for the exhaustive per-dialect/per-level wire tests.
# This file keeps only the ONE smoke test proving the full kwargs surface
# carries BOTH transport and thinking together end-to-end.
# --------------------------------------------------------------------------- #
def test_provider_lm_kwargs_carries_transport_and_thinking() -> None:
    """The full provider kwargs surface carries BOTH transport and thinking."""
    invalidation.record_endpoint_capabilities(
        EndpointCapabilities(
            provider_id="claude_code",
            api_base="claude-code://sdk",
            dialect="claude_code",
            thinking_controls=Fact(frozenset({"claude_code_thinking"}), "dialect", _NOW),
        )
    )
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="test:claude_code:haiku",
            thinking=Fact(ThinkingSpec(mechanism="budget_tokens"), "server_report", _NOW),
        )
    )
    from clio_agent.providers.capabilities.records import DeploymentCapabilities

    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="claude_code",
            api_base="claude-code://sdk",
            model_id="haiku",
            model_key=Fact("test:claude_code:haiku", "server_report", _NOW),
        )
    )
    extras = build_request_kwargs(
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="off")
    )
    assert extras["claude_code_transport"] == "sdk"
    assert extras["claude_code_thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- #
# The option reaches the real SDK options object.
# --------------------------------------------------------------------------- #
def test_build_sdk_options_places_thinking_on_real_sdk_options() -> None:
    pytest.importorskip("claude_agent_sdk")
    opts = build_sdk_options(model="haiku", cwd=None, stream=True, thinking={"type": "disabled"})
    assert opts.thinking == {"type": "disabled"}
    assert opts.include_partial_messages is True

    enabled = {"type": "enabled", "budget_tokens": 2048}
    assert (
        build_sdk_options(model="haiku", cwd=None, stream=False, thinking=enabled).thinking
        == enabled
    )

    # Unset → the SDK field stays None (provider/CLI default governs).
    assert build_sdk_options(model="haiku", cwd=None, stream=False, thinking=None).thinking is None


def test_build_sdk_options_isolates_clio_from_personal_claude_capabilities() -> None:
    """CLIO's provider path never imports user MCPs, plugins, or SDK skills."""
    pytest.importorskip("claude_agent_sdk")
    opts = build_sdk_options(model="sonnet", cwd=None, stream=True, thinking=None)

    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.strict_mcp_config is True
    assert opts.setting_sources == []
    assert opts.skills == []
    assert opts.plugins == []


# --------------------------------------------------------------------------- #
# The factory hands the thinking config to the engine, and the engine hands it to
# the SDK client's options. (SABOTAGE: drop ``thinking=thinking`` in
# ``factory._claude_code_lm`` or at the engine's ``entry.stream`` call -> red.)
# --------------------------------------------------------------------------- #
def test_the_thinking_config_reaches_the_sdk_client_options(monkeypatch) -> None:
    import asyncio

    from dspy.lm15 import Message, Request

    from clio_agent.lm.factory import create_lm
    from tests import _fake_claude_sdk as fake

    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    try:
        sdk = fake.install(monkeypatch)
        config = LMProviderConfig(provider="claude_code", model="claude-sonnet-4-6")
        config.thinking_level = "medium"  # type: ignore[attr-defined]
        lm = create_lm(config)
        thinking = lm._async_engine_spec.thinking
        assert thinking == build_request_kwargs(config)["claude_code_thinking"]

        request = Request(model=lm.model, messages=(Message.user("hi"),))
        asyncio.run(lm._async_engine_spec.complete(request))
        options = sdk.clients[0].options.kwargs
        assert options["thinking"] == {k: v for k, v in thinking.items() if k != "effort"}
        assert options["effort"] == thinking["effort"] == "medium"
    finally:
        _reset_sessions_for_tests()
        claude_code_engine._CONVERSATIONS.clear_for_tests()


# --------------------------------------------------------------------------- #
# Session pool re-keys on distinct thinking configs.
# --------------------------------------------------------------------------- #
def test_thinking_key_is_stable_and_distinct() -> None:
    assert thinking_key(None) is None
    assert thinking_key({}) is None
    a = thinking_key({"type": "enabled", "budget_tokens": 2048})
    b = thinking_key({"budget_tokens": 2048, "type": "enabled"})  # key order irrelevant
    assert a == b
    assert a != thinking_key({"type": "disabled"})


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A minimal fake ``claude_agent_sdk`` -- connect/disconnect only."""
    state: dict[str, Any] = {"constructed": 0}

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

        async def set_model(self, model: str | None) -> None:
            return None

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


async def test_entry_reconnects_on_distinct_thinking_config(monkeypatch) -> None:
    """S2 B13: a session's entry keeps ONE client across same-config calls, but a
    thinking-config change on the SAME entry reconnects (this SDK exposes no
    live ``set_thinking``/``set_effort`` control request — only ``set_model``)."""
    from clio_agent.providers.claude_code_sessions import _StreamClientEntry

    state = _install_fake_sdk(monkeypatch)
    entry = _StreamClientEntry()
    await entry._ensure_client(lambda: None, model="haiku", cwd="/w", thinking={"type": "disabled"})
    await entry._ensure_client(lambda: None, model="haiku", cwd="/w", thinking={"type": "disabled"})
    assert state["constructed"] == 1  # identical config -> no reconnect

    await entry._ensure_client(
        lambda: None,
        model="haiku",
        cwd="/w",
        thinking={"type": "enabled", "budget_tokens": 2048},
    )
    assert state["constructed"] == 2  # distinct thinking config -> reconnect

    await entry._ensure_client(lambda: None, model="haiku", cwd="/w", thinking={"type": "disabled"})
    assert state["constructed"] == 3  # reconnects back too


# --------------------------------------------------------------------------- #
# Config + env plumbing.
# --------------------------------------------------------------------------- #
def test_config_validates_thinking_level() -> None:
    assert (
        LMProviderConfig(
            provider="claude_code", model="haiku", thinking_level="HIGH"
        ).thinking_level
        == "high"
    )
    with pytest.raises(ValueError, match="thinking_level must be"):
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="extreme")


def test_thinking_level_and_budget_from_env() -> None:
    env = {
        "CLIO_LM_PROVIDER": "claude_code",
        "CLIO_LM_THINKING_LEVEL": "low",
        "CLIO_LM_THINKING_BUDGET": "3000",
    }
    with isolated_environ(env):
        config = load_config_from_env()
    assert config.thinking_level == "low"
    assert config.thinking_budget == 3000


def test_unsupported_dialect_logs_via_the_request_builder(caplog) -> None:
    """The same "no silent no-op" contract holds end-to-end through
    ``build_request_kwargs`` for a dialect with NO mapping at all (neither the
    legacy SDK/CLI engine nor a dialect_wire entry)."""
    cfg = LMProviderConfig(provider="argonne", model="m")
    cfg.provider = "mystery"  # type: ignore[assignment]
    cfg.provider_id = "mystery"
    cfg.thinking_level = "high"
    with caplog.at_level(logging.WARNING, logger="clio_agent.lm.request_builder"):
        extras = build_request_kwargs(cfg)
    assert "claude_code_thinking" not in extras
    assert "reasoning_effort" not in extras
    assert any("thinking_unsupported" in r.message for r in caplog.records)
