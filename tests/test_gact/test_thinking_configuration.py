"""Thinking preferences reuse a live binding; actual connection changes do not."""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from clio_agent.config import LMProviderConfig
from clio_agent.gact.providers.bind_configuration import (
    _thinking_configuration,
    prepare_bind_configuration,
)
from clio_agent.gact.types import LMProviderRequest


def _bound(
    provider: str = "codex", model: str = "gpt-6-luna"
) -> tuple[FastAPI, LMProviderConfig, LMProviderRequest]:
    cfg = LMProviderConfig(
        provider=provider, model=model, thinking_level="medium", api_key="saved-key"
    )
    cfg.context_window = 272000
    cfg.chosen_context = 272000
    cfg.native_tool_calling = True
    app = FastAPI()
    app.state.agent = SimpleNamespace(_provider_config=cfg)
    app.state.lm_config = None
    req = LMProviderRequest(
        provider=cfg.provider,
        provider_id=cfg.provider_id,
        api_base=cfg.api_base,
        model=cfg.model,
        thinking_level="low",
    )
    return app, cfg, req


@pytest.mark.asyncio
async def test_thinking_change_reuses_bound_credentials_and_capabilities() -> None:
    app, old, req = _bound()
    app.state.provider_catalog_startup_task = SimpleNamespace(done=lambda: False)

    def unexpected_readiness() -> tuple[str, str, bool, str]:
        raise AssertionError("A bound model's thinking preference does not probe sign-in")

    cfg, thinking_only = await prepare_bind_configuration(
        app, req, unexpected_readiness, unexpected_readiness
    )
    assert thinking_only
    assert cfg is not old
    assert cfg.thinking_level == "low"
    assert old.thinking_level == "medium"
    assert cfg.api_key == "saved-key"
    assert cfg.context_window == cfg.chosen_context == 272000
    assert cfg.native_tool_calling


def test_thinking_reset_restores_the_same_models_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, old, req = _bound("claude_code", "sonnet")
    req = req.model_copy(update={"thinking_level": None})

    def shipped_default(model: str) -> str:
        assert model == "sonnet"
        return "low"

    monkeypatch.setattr(
        "clio_agent.providers.capabilities.dialects.claude_code.shipped_default_effort_for_model",
        shipped_default,
    )
    cfg = _thinking_configuration(app, req)
    assert cfg is not None
    assert cfg.thinking_level == "low"
    assert old.thinking_level == "medium"
    monkeypatch.setattr(
        "clio_agent.providers.capabilities.dialects.claude_code.shipped_default_effort_for_model",
        lambda _model: None,
    )
    unknown_default = _thinking_configuration(app, req)
    assert unknown_default is not None
    assert unknown_default.thinking_level is None


def test_connection_or_other_setting_changes_cannot_use_the_thinking_path() -> None:
    app, _, req = _bound()
    for change in (
        {"model": "gpt-6-sol"},
        {"api_base": "https://another.example/v1"},
        {"api_key": "new-key"},
        {"temperature": 0.5},
        {"context_length": 16000},
        {"transport": "sse"},
        {"provider_options": {"region": "other"}},
        {"thinking_budget": 2048},
        {"turn_timeout_s": 60},
    ):
        assert _thinking_configuration(app, req.model_copy(update=change)) is None, change
    omitted = LMProviderRequest.model_validate(req.model_dump(exclude={"thinking_level"}))
    assert _thinking_configuration(app, omitted) is None


@pytest.mark.asyncio
async def test_actual_model_change_still_runs_readiness_and_forced_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, req = _bound()
    calls: list[Any] = []

    def readiness() -> tuple[str, str, bool, str]:
        calls.append("readiness")
        return "ready", "", True, "gpt-6-luna"

    async def handshake(context: Any, **kwargs: Any) -> None:
        calls.append((context.target_model, kwargs))

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", handshake)
    cfg, thinking_only = await prepare_bind_configuration(
        app, req.model_copy(update={"model": "gpt-6-sol"}), readiness, readiness
    )
    assert not thinking_only
    assert cfg.model == "gpt-6-sol"
    assert calls == ["readiness", ("gpt-6-sol", {"force": True})]
