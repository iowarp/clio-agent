"""#1455: a Claude Code sign-in made in a terminal is seen without pressing Check.

The re-ask is negotiated (message accepted, provider list loaded), never a
timer, runs only while CLIO's cache says "not verified", and is single-flight.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import claude_code_auth_reprobe as reprobe
from clio_agent.providers import model_discovery
from clio_agent.providers.model_discovery import claude_code as cc_discovery

_CLAUDE = SimpleNamespace(id="claude_code", provider_kind="claude_code")


def _app(**state: Any) -> Any:
    return SimpleNamespace(state=SimpleNamespace(**state))


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake CLI + overlay: tests set whether the CLI reports a sign-in."""

    world: dict[str, Any] = {"verified": False, "signed_in": False, "status_calls": 0}
    refreshed: list[Any] = []
    invalidated: list[str] = []

    def _verified() -> tuple[bool, str, str]:
        return world["verified"], "", ""

    def _auth_status(binary: str, *, timeout: float) -> tuple[bool, str]:
        world["status_calls"] += 1
        return world["signed_in"], "" if world["signed_in"] else "not signed in"

    async def _refresh_all(presets: list[Any]) -> list[Any]:
        refreshed.extend(presets)
        world["verified"] = True  # a signed-in refresh writes a verified overlay
        return []

    monkeypatch.setattr(reprobe, "claude_code_overlay_verified", _verified)
    monkeypatch.setattr(cc_discovery, "_auth_status", _auth_status)
    monkeypatch.setattr(cc_discovery, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(model_discovery, "refresh_all", _refresh_all)
    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog_snapshot.invalidate_provider",
        lambda app, provider_id: invalidated.append(provider_id),
    )
    world["refreshed"], world["invalidated"] = refreshed, invalidated
    return world


async def test_a_verified_cache_is_not_re_asked(cli: dict[str, Any]) -> None:
    cli["verified"] = True

    assert await reprobe.reprobe_claude_code_auth(_app(), _CLAUDE, trigger="message") is True
    assert cli["status_calls"] == 0


async def test_still_signed_out_runs_only_the_cheap_status_check(cli: dict[str, Any]) -> None:
    assert await reprobe.reprobe_claude_code_auth(_app(), _CLAUDE, trigger="message") is False
    assert cli["status_calls"] == 1
    assert cli["refreshed"] == []


async def test_a_terminal_sign_in_refreshes_the_provider(cli: dict[str, Any]) -> None:
    cli["signed_in"] = True

    assert await reprobe.reprobe_claude_code_auth(_app(), _CLAUDE, trigger="message") is True
    assert cli["refreshed"] == [_CLAUDE]
    assert cli["invalidated"] == ["claude_code"]


async def test_concurrent_callers_share_one_probe(cli: dict[str, Any]) -> None:
    cli["signed_in"] = True
    app = _app()

    results = await asyncio.gather(
        *(reprobe.reprobe_claude_code_auth(app, _CLAUDE, trigger="provider_list") for _ in range(4))
    )

    assert results == [True] * 4
    assert cli["status_calls"] == 1


async def test_the_startup_check_is_not_duplicated(cli: dict[str, Any]) -> None:
    loop = asyncio.get_running_loop()
    running = loop.create_future()
    app = _app(provider_catalog_startup_task=running)

    assert await reprobe.reprobe_claude_code_auth(app, _CLAUDE, trigger="message") is False
    assert cli["status_calls"] == 0
    running.cancel()


async def test_other_providers_are_never_probed(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await reprobe.reprobe_claude_code_for_provider(_app(), "openrouter", trigger="message")
    await reprobe.reprobe_claude_code_for_provider(_app(), "", trigger="message")

    assert cli["status_calls"] == 0


def test_message_provider_follows_message_then_session_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.providers.config._effective_lm_config",
        lambda app: {"provider_id": "codex"},
    )
    sessions = {"s": SimpleNamespace(model={"provider_id": "claude_code", "model_id": "sonnet"})}
    app = _app(sessions=sessions)

    with_model = SimpleNamespace(model={"provider_id": "openrouter", "model_id": "x"})
    assert reprobe._message_provider_id(app, "s", with_model) == "openrouter"
    assert reprobe._message_provider_id(app, "s", SimpleNamespace(model=None)) == "claude_code"
    assert reprobe._message_provider_id(app, "none", SimpleNamespace(model=None)) == "codex"
