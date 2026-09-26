"""Tests for the generic provider sign-in API dispatch (start/complete/status/logout, A.9).

Exercises :func:`clio_agent.gact.routes.provider_auth.handle_auth_action`
directly for both provider kinds it supports today: ``argonne`` (ALCF) and
``codex``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from clio_agent.gact.lm_provider_types import LMProviderPreset
from clio_agent.gact.routes.provider_auth import handle_auth_action, supports_logout
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.model_discovery import resolve_cloud_api_key


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
def _reset_codex_flows() -> None:
    from clio_agent.providers.codex.login_flow import _reset_login_flows_for_tests

    _reset_login_flows_for_tests()
    yield
    _reset_login_flows_for_tests()


@pytest.fixture(autouse=True)
def _no_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.routes.provider_auth.invalidate_provider", lambda *a, **k: None
    )


def test_supports_logout_matches_the_registry_exactly() -> None:
    """The registry IS the answer -- argonne and codex have real logout
    handlers, Claude Code (the user's own CLI login) does not, and neither
    does an unknown kind."""
    assert supports_logout("argonne") is True
    assert supports_logout("codex") is True
    assert supports_logout("claude_code") is False
    assert supports_logout("openai") is False


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

    preset = _preset(provider="codex")
    with pytest.raises(HTTPException) as exc_info:
        await handle_auth_action(
            preset=preset, action="bogus", body={}, app=_FakeApp(), presets=[preset]
        )
    assert exc_info.value.status_code == 400


def _api_key_preset() -> LMProviderPreset:
    return LMProviderPreset(
        id="openrouter",
        provider="openai",
        label="OpenRouter",
        api_base="",
        suggested_model="",
        requires_api_key=True,
        api_key_env="OPENROUTER_API_KEY",
    )


class TestApiKeySaveAndClear:
    """save_api_key/clear_api_key: the non-binding counterpart to PUT
    /v1/providers/lm (#1446 follow-up) -- saving a key must never change
    which provider is active, only whether THIS one is checkable."""

    async def test_save_api_key_persists_to_the_durable_store_never_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        preset = _api_key_preset()

        result = await handle_auth_action(
            preset=preset,
            action="save_api_key",
            body={"api_key": "sk-test-123"},
            app=_FakeApp(),
            presets=[preset],
        )

        assert result == {
            "provider_id": "openrouter",
            "is_authenticated": True,
            "instructions": "Saved the OpenRouter API key. Checking available models.",
        }
        assert ProviderApiKeyStore().load("openrouter") == "sk-test-123"
        assert resolve_cloud_api_key("openrouter") == "sk-test-123"
        assert "OPENROUTER_API_KEY" not in os.environ

    async def test_save_api_key_requires_a_non_empty_key(self) -> None:
        preset = _api_key_preset()
        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset, action="save_api_key", body={}, app=_FakeApp(), presets=[preset]
            )
        assert exc_info.value.status_code == 400

    async def test_save_api_key_on_a_provider_that_does_not_use_one_is_405(self) -> None:
        preset = _preset(provider="claude_code")
        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset,
                action="save_api_key",
                body={"api_key": "x"},
                app=_FakeApp(),
                presets=[preset],
            )
        assert exc_info.value.status_code == 405

    async def test_clear_api_key_removes_the_saved_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        ProviderApiKeyStore().save("openrouter", "sk-test-123")
        preset = _api_key_preset()

        result = await handle_auth_action(
            preset=preset, action="clear_api_key", body={}, app=_FakeApp(), presets=[preset]
        )

        assert result == {
            "provider_id": "openrouter",
            "is_authenticated": False,
            "instructions": "Removed the OpenRouter API key.",
        }
        assert ProviderApiKeyStore().load("openrouter") == ""
        assert resolve_cloud_api_key("openrouter") == ""

    async def test_clear_api_key_reports_an_operator_env_key_that_still_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
        ProviderApiKeyStore().save("openrouter", "sk-saved")
        preset = _api_key_preset()

        result = await handle_auth_action(
            preset=preset, action="clear_api_key", body={}, app=_FakeApp(), presets=[preset]
        )

        assert result["is_authenticated"] is True
        assert resolve_cloud_api_key("openrouter") == "sk-from-env"

    async def test_clear_api_key_on_a_provider_that_does_not_use_one_is_405(self) -> None:
        preset = _preset(provider="claude_code")
        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset, action="clear_api_key", body={}, app=_FakeApp(), presets=[preset]
            )
        assert exc_info.value.status_code == 405

    async def test_saving_a_key_never_touches_the_active_bind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the split: unlike PUT /v1/providers/lm, this
        route takes no `app.state.agent` / profile-store argument at all --
        there is nothing here that COULD rebind the active provider."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        preset = _api_key_preset()
        app = _FakeApp()
        before = vars(app.state).copy()

        await handle_auth_action(
            preset=preset,
            action="save_api_key",
            body={"api_key": "sk-test-456"},
            app=app,
            presets=[preset],
        )

        assert vars(app.state) == before


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
            lambda **_kwargs: argonne_auth.PendingAuthentication(
                flow_id="flow_1", authorization_url="https://globus/auth"
            ),
        )
        preset = _preset(provider="argonne")
        result = await handle_auth_action(
            preset=preset, action="start", body={}, app=_FakeApp(), presets=[preset]
        )
        assert result["flow_id"] == "flow_1"
        assert result["browser"] == {"authorization_url": "https://globus/auth", "loopback": False}

    async def test_start_with_force_requests_a_fresh_globus_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit (re)click always forces `prompt=login` -- the ONE action
        for `argonne_reauthentication_required` ("Sign in again") never
        silently re-uses a browser session that produced a rejected
        credential."""
        from clio_agent.providers import argonne_auth

        monkeypatch.setattr(
            "clio_agent.gact.routes.provider_auth.ensure_argonne_support", lambda: False
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            argonne_auth,
            "begin_authentication",
            lambda **kwargs: calls.append(kwargs)
            or argonne_auth.PendingAuthentication(
                flow_id="flow_1", authorization_url="https://globus/auth"
            ),
        )
        preset = _preset(provider="argonne")
        await handle_auth_action(
            preset=preset, action="start", body={"force": True}, app=_FakeApp(), presets=[preset]
        )
        assert calls == [{"force_login": True}]

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

    async def test_logout_revokes_and_reports_signed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's live-tested defect: ALCF sign-out used to 405. It now
        revokes through the SDK's own sign-out primitive and reports
        `is_authenticated: False`."""
        from clio_agent.providers import argonne_auth

        calls: list[None] = []
        monkeypatch.setattr(argonne_auth, "sign_out", lambda: calls.append(None))
        preset = _preset(provider="argonne")

        result = await handle_auth_action(
            preset=preset, action="logout", body={}, app=_FakeApp(), presets=[preset]
        )

        assert result["is_authenticated"] is False
        assert calls == [None]

    async def test_logout_failure_is_a_typed_502_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from clio_agent.providers import argonne_auth

        def _fail() -> None:
            raise RuntimeError("globus unreachable")

        monkeypatch.setattr(argonne_auth, "sign_out", _fail)
        preset = _preset(provider="argonne")

        with pytest.raises(HTTPException) as exc_info:
            await handle_auth_action(
                preset=preset, action="logout", body={}, app=_FakeApp(), presets=[preset]
            )
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"]["error"] == "argonne_logout_failed"


class TestCodex:
    async def test_start_browser_returns_methods(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clio_agent.providers.codex import login_flow

        preset = _preset(provider="codex")
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

        preset = _preset(provider="codex")
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

        from clio_agent.providers.codex import login_flow

        preset = _preset(provider="codex")
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
        from clio_agent.providers.codex import login_flow
        from clio_agent.providers.codex.credentials import CodexCredentialStore

        store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
        monkeypatch.setattr(
            "clio_agent.providers.codex.credentials.CodexCredentialStore", lambda: store
        )
        flow = login_flow.CodexLoginFlow(flow_id="flow-1")
        flow._credential = CodexCredential(  # noqa: SLF001 - simulate a completed exchange
            access_token="at", refresh_token="rt", expires_at_ms=0, account_id="acct_1"
        )
        with flow._result.lock:  # noqa: SLF001
            flow._result.status = "complete"  # noqa: SLF001
        login_flow._set_current_flow_for_tests(flow)  # noqa: SLF001

        preset = _preset(provider="codex")
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
        from clio_agent.providers.codex.credentials import CodexCredentialStore

        store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
        store.save(
            CodexCredential(access_token="at", refresh_token="rt", expires_at_ms=0, account_id="a")
        )
        monkeypatch.setattr(
            "clio_agent.providers.codex.credentials.CodexCredentialStore", lambda: store
        )
        preset = _preset(provider="codex")
        result = await handle_auth_action(
            preset=preset, action="logout", body={}, app=_FakeApp(), presets=[preset]
        )
        assert result["is_authenticated"] is False
        assert store.is_signed_in() is False
