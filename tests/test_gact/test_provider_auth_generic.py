"""Tests for the generic provider sign-in API dispatch (start/complete/status/logout, A.9).

Exercises :func:`clio_agent.gact.routes.provider_auth.handle_auth_action`
directly for both provider kinds it supports today: ``argonne`` (ALCF) and
``chatgpt``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.lm_provider_types import LMProviderPreset
from clio_agent.gact.routes.provider_auth import handle_auth_action
from clio_agent.providers.chatgpt.login_flow import ChatGptCredential


def _preset(*, provider: str, provider_id: str = "") -> LMProviderPreset:
    return LMProviderPreset(
        id=provider_id or provider,
        provider=provider,
        label=provider,
        api_base="",
        suggested_model="",
        requires_api_key=False,
    )


class _FakeApp:
    def __init__(self) -> None:
        self.state = SimpleNamespace()


@pytest.fixture(autouse=True)
def _reset_chatgpt_flows() -> None:
    from clio_agent.providers.chatgpt.login_flow import _reset_login_flows_for_tests

    _reset_login_flows_for_tests()
    yield
    _reset_login_flows_for_tests()


@pytest.fixture(autouse=True)
def _no_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.routes.provider_auth.invalidate_provider", lambda *a, **k: None
    )


async def test_unknown_provider_kind_returns_405_for_start() -> None:
    from fastapi import HTTPException

    preset = _preset(provider="openai")
    with pytest.raises(HTTPException) as exc_info:
        await handle_auth_action(
            preset=preset, action="start", body={}, app=_FakeApp(), presets=[preset]
        )
    assert exc_info.value.status_code == 405


async def test_invalid_action_returns_400() -> None:
    from fastapi import HTTPException

    preset = _preset(provider="chatgpt")
    with pytest.raises(HTTPException) as exc_info:
        await handle_auth_action(
            preset=preset, action="bogus", body={}, app=_FakeApp(), presets=[preset]
        )
    assert exc_info.value.status_code == 400


class TestArgonne:
    async def test_start_returns_browser_url_and_flow_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clio_agent.providers import argonne_auth

        monkeypatch.setattr(
            "clio_agent.gact.routes.provider_auth.ensure_argonne_support", lambda: False
        )
        monkeypatch.setattr(
            argonne_auth,
            "begin_authentication",
            lambda: argonne_auth.PendingAuthentication(
                flow_id="flow_1", authorization_url="https://globus/auth"
            ),
        )
        preset = _preset(provider="argonne")
        result = await handle_auth_action(
            preset=preset, action="start", body={}, app=_FakeApp(), presets=[preset]
        )
        assert result["flow_id"] == "flow_1"
        assert result["browser"] == {"authorization_url": "https://globus/auth", "loopback": False}

    async def test_complete_calls_complete_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clio_agent.providers import argonne_auth

        calls: dict[str, Any] = {}

        def _complete(flow_id: str, code: str) -> None:
            calls["flow_id"] = flow_id
            calls["code"] = code

        monkeypatch.setattr(argonne_auth, "complete_authentication", _complete)
        preset = _preset(provider="argonne")
        result = await handle_auth_action(
            preset=preset,
            action="complete",
            body={"flow_id": "flow_1", "authorization_code": "code123"},
            app=_FakeApp(),
            presets=[preset],
        )
        assert result["is_authenticated"] is True
        assert calls == {"flow_id": "flow_1", "code": "code123"}

    async def test_status_reflects_pending_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clio_agent.providers import argonne_auth

        monkeypatch.setattr(
            argonne_auth, "flow_is_pending", lambda flow_id: flow_id == "still_pending"
        )
        preset = _preset(provider="argonne")
        pending = await handle_auth_action(
            preset=preset,
            action="status",
            body={"flow_id": "still_pending"},
            app=_FakeApp(),
            presets=[preset],
        )
        assert pending["state"] == "pending"
        done = await handle_auth_action(
            preset=preset,
            action="status",
            body={"flow_id": "unknown"},
            app=_FakeApp(),
            presets=[preset],
        )
        assert done["state"] == "complete"

    async def test_logout_is_405_unsupported(self) -> None:
        from fastapi import HTTPException

        preset = _preset(provider="argonne")
        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset, action="logout", body={}, app=_FakeApp(), presets=[preset]
            )
        assert exc_info.value.status_code == 405


class TestChatGpt:
    async def test_start_browser_returns_methods(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clio_agent.providers.chatgpt import login_flow

        preset = _preset(provider="chatgpt")
        result = await handle_auth_action(
            preset=preset, action="start", body={}, app=_FakeApp(), presets=[preset]
        )
        try:
            assert "flow_id" in result
            assert "browser" in result
            assert "authorization_url" in result["browser"]
        finally:
            flow = login_flow.get_login_flow(result["flow_id"])
            if flow is not None:
                flow.cancel()

    async def test_complete_with_bad_flow_id_is_404(self) -> None:
        from fastapi import HTTPException

        preset = _preset(provider="chatgpt")
        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset,
                action="complete",
                body={"flow_id": "does-not-exist", "paste": "abc123"},
                app=_FakeApp(),
                presets=[preset],
            )
        assert exc_info.value.status_code == 404

    async def test_complete_with_bad_paste_is_401(self) -> None:
        from fastapi import HTTPException

        from clio_agent.providers.chatgpt import login_flow

        preset = _preset(provider="chatgpt")
        start_result = await handle_auth_action(
            preset=preset, action="start", body={}, app=_FakeApp(), presets=[preset]
        )
        flow = login_flow.get_login_flow(start_result["flow_id"])
        assert flow is not None
        flow.cancel()  # closes the loopback listener so submit_paste is the only path left

        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset,
                action="complete",
                body={"flow_id": start_result["flow_id"], "paste": ""},
                app=_FakeApp(),
                presets=[preset],
            )
        assert exc_info.value.status_code == 401

    async def test_status_persists_credential_and_drops_flow_on_completion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from clio_agent.providers.chatgpt import login_flow
        from clio_agent.providers.chatgpt.credentials import ChatGptCredentialStore

        store = ChatGptCredentialStore(path=tmp_path / "chatgpt_credential.json")
        monkeypatch.setattr(
            "clio_agent.providers.chatgpt.credentials.ChatGptCredentialStore", lambda: store
        )
        flow = login_flow.create_login_flow()
        flow._credential = ChatGptCredential(  # noqa: SLF001 - simulate a completed exchange
            access_token="at", refresh_token="rt", expires_at_ms=0, account_id="acct_1"
        )
        with flow._result.lock:  # noqa: SLF001
            flow._result.status = "complete"  # noqa: SLF001

        preset = _preset(provider="chatgpt")
        result = await handle_auth_action(
            preset=preset,
            action="status",
            body={"flow_id": flow.flow_id},
            app=_FakeApp(),
            presets=[preset],
        )
        assert result["state"] == "complete"
        assert store.is_signed_in() is True
        assert login_flow.get_login_flow(flow.flow_id) is None

    async def test_logout_deletes_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from clio_agent.providers.chatgpt.credentials import ChatGptCredentialStore

        store = ChatGptCredentialStore(path=tmp_path / "chatgpt_credential.json")
        store.save(
            ChatGptCredential(
                access_token="at", refresh_token="rt", expires_at_ms=0, account_id="a"
            )
        )
        monkeypatch.setattr(
            "clio_agent.providers.chatgpt.credentials.ChatGptCredentialStore", lambda: store
        )
        preset = _preset(provider="chatgpt")
        result = await handle_auth_action(
            preset=preset, action="logout", body={}, app=_FakeApp(), presets=[preset]
        )
        assert result["is_authenticated"] is False
        assert store.is_signed_in() is False
