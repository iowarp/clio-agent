"""F042: a server booted from the persisted bind reports the discovered window."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.providers import boot_handshake


class _Report:
    ok = True
    provider_id = "ollama"
    api_base = "http://127.0.0.1:11434/v1"


def _app_and_agent() -> tuple[Any, Any]:
    cfg = LMProviderConfig(
        provider="ollama", provider_id="ollama", api_base=_Report.api_base, model="qwen3:4b"
    )
    agent = SimpleNamespace(_provider_config=cfg)
    app = SimpleNamespace(state=SimpleNamespace(agent=agent, lm_handshake_report=None))
    return app, agent


def _fake_handshake(monkeypatch: pytest.MonkeyPatch, seen: list[Any]) -> None:
    async def run_handshake(ctx: Any, **_: Any) -> _Report:
        seen.append(ctx)
        return _Report()

    def apply_handshake(self: LMProviderConfig, report: Any, **_: Any) -> None:
        self.context_window = self.chosen_context = 253952

    import clio_agent.providers.handshake as handshake

    monkeypatch.setattr(handshake, "run_handshake", run_handshake)
    monkeypatch.setattr(LMProviderConfig, "apply_handshake", apply_handshake)


def test_boot_agent_gets_the_discovered_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    _fake_handshake(monkeypatch, seen)
    app, agent = _app_and_agent()

    asyncio.run(boot_handshake.refresh_bound_window(app, agent))

    assert agent._provider_config.context_window == 253952
    assert isinstance(app.state.lm_handshake_report, _Report)
    assert seen[0].target_model == "qwen3:4b" and seen[0].auth_mode == "passive"


def test_a_bind_during_discovery_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    _fake_handshake(monkeypatch, seen)
    app, agent = _app_and_agent()
    rebound = object()

    async def run() -> None:
        app.state.agent = SimpleNamespace(_provider_config=None)  # a PUT swapped the agent
        app.state.lm_handshake_report = rebound
        await boot_handshake.refresh_bound_window(app, agent)

    asyncio.run(run())

    assert agent._provider_config.context_window is None
    assert app.state.lm_handshake_report is rebound


def test_a_failed_discovery_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_handshake(ctx: Any, **_: Any) -> None:
        raise ConnectionError("refused")

    import clio_agent.providers.handshake as handshake

    monkeypatch.setattr(handshake, "run_handshake", run_handshake)
    app, agent = _app_and_agent()

    asyncio.run(boot_handshake.refresh_bound_window(app, agent))

    assert "refused" in app.state.boot_handshake_error
    assert agent._provider_config.context_window is None


def test_no_model_bound_skips_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    _fake_handshake(monkeypatch, seen)
    app, agent = _app_and_agent()
    agent._provider_config.model = ""

    asyncio.run(boot_handshake.refresh_bound_window(app, agent))

    assert seen == []
