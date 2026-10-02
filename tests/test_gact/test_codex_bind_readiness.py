"""A Codex bind right after launch waits for the model check instead of refusing.

Choosing a Codex model in the first seconds used to answer 401 "Codex models are
being checked" while the startup check was about to finish; and with no startup
check at all a signed-in Codex login was refused as "not validated". The bind now
waits (bounded) for an in-flight check, and checks the models itself when nothing
has, as the Claude Code bind does.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clio_agent.gact.routes import codex_readiness
from clio_agent.gact.routes.codex_readiness import apply_codex_readiness_gate, await_startup_check


def _cfg() -> Any:
    return SimpleNamespace(model="")


async def test_a_bind_waits_for_the_startup_check_in_flight() -> None:
    loop = asyncio.get_running_loop()
    check = loop.create_future()
    loop.call_later(0.05, check.set_result, None)
    await await_startup_check(
        SimpleNamespace(state=SimpleNamespace(provider_catalog_startup_task=check))
    )
    assert check.done()


async def test_the_wait_is_bounded_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bounded wait falling through is a WARNING with a typed reason (#1577).

    SABOTAGE: drop the warning when the wait falls through -> no record -> red.
    """
    monkeypatch.setattr(codex_readiness, "STARTUP_CHECK_WAIT_S", 0.01)
    never = asyncio.get_running_loop().create_future()
    with caplog.at_level(logging.WARNING, logger=codex_readiness.__name__):
        await await_startup_check(
            SimpleNamespace(state=SimpleNamespace(provider_catalog_startup_task=never))
        )
    assert not never.done()
    never.cancel()
    assert any("reason=startup_check_still_running" in r.getMessage() for r in caplog.records)


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


# --------------------------------------------------------------------------- #
# The real bind route: Codex is direct-only                                    #
# --------------------------------------------------------------------------- #
def _codex_bind(variant: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": "codex",
        "api_base": "codex://direct",
        "model": "gpt-5.6-sol",
        "api_key": "x",
    }
    if variant is not None:
        body["variant"] = variant
    return body


@pytest.fixture
def signed_out_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real app with no Codex sign-in anywhere (no CLIO credential, empty CODEX_HOME)."""
    from clio_agent.gact.app import build_app
    from clio_agent.providers.codex.credentials import CodexCredentialStore

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: False)
    return build_app(sessions_path=tmp_path / "s.json")


def test_binding_codex_with_no_sign_in_is_the_typed_401(signed_out_app: Any) -> None:
    with TestClient(signed_out_app) as client:
        response = client.put("/v1/providers/lm", json=_codex_bind())
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error"] == "codex_auth_required"


@pytest.mark.parametrize("variant", ["sdk", "direct"])
def test_binding_codex_with_any_variant_is_refused(signed_out_app: Any, variant: str) -> None:
    with TestClient(signed_out_app) as client:
        refused = client.put("/v1/providers/lm", json=_codex_bind(variant))
    assert refused.status_code == 422, refused.text
    assert "Model variants are no longer used for Codex" in refused.text
