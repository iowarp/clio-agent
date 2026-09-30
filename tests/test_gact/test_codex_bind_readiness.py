"""A Codex bind right after launch waits for the model check instead of refusing.

Choosing a Codex model in the first seconds used to answer 401 "Codex models are
being checked" while the startup check was about to finish; and with no startup
check at all a signed-in Codex login was refused as "not validated". The bind now
waits (bounded) for an in-flight check, and checks the models itself when nothing
has, as the Claude Code bind does.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from clio_agent.gact.routes import codex_variant
from clio_agent.gact.routes.codex_variant import apply_codex_readiness_gate, await_startup_check


def _cfg(variant: str = "direct") -> Any:
    return SimpleNamespace(codex_variant=variant, model="")


async def test_a_bind_waits_for_the_startup_check_in_flight() -> None:
    loop = asyncio.get_running_loop()
    check = loop.create_future()
    loop.call_later(0.05, check.set_result, None)
    await await_startup_check(
        SimpleNamespace(state=SimpleNamespace(provider_catalog_startup_task=check))
    )
    assert check.done()


async def test_the_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_variant, "STARTUP_CHECK_WAIT_S", 0.01)
    never = asyncio.get_running_loop().create_future()
    await await_startup_check(
        SimpleNamespace(state=SimpleNamespace(provider_catalog_startup_task=never))
    )
    assert not never.done()
    never.cancel()


async def test_no_startup_check_is_no_wait() -> None:
    await await_startup_check(SimpleNamespace(state=SimpleNamespace()))


async def test_an_unchecked_sign_in_is_checked_then_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import model_discovery

    checked: list[Any] = []
    state = {"verified": False}

    async def _refresh_all(presets: list[Any]) -> list[Any]:
        checked.extend(presets)
        state["verified"] = True
        return []

    def _readiness() -> tuple[str, str, bool, str]:
        if state["verified"]:
            return "ready", "ok", True, "gpt-x"
        return "auth_check_required", "present but not validated", False, ""

    monkeypatch.setattr(model_discovery, "refresh_all", _refresh_all)
    cfg = _cfg()
    await apply_codex_readiness_gate(cfg, SimpleNamespace(model=""), _readiness)
    assert [p.id for p in checked] == ["codex"]
    assert cfg.model == "gpt-x"


async def test_a_signed_out_login_is_still_refused_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _readiness() -> tuple[str, str, bool, str]:
        return "auth_required", "sign in to Codex", False, ""

    with pytest.raises(HTTPException) as err:
        await apply_codex_readiness_gate(_cfg(), SimpleNamespace(model=""), _readiness)
    assert err.value.status_code == 401
    assert err.value.detail["error"]["error"] == "codex_auth_required"
