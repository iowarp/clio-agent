"""Tests for Argonne / ALCF auth helper behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers import argonne_auth


class _FakeAuthParams:
    def __init__(self, session_required_single_domain: Any = None) -> None:
        self.session_required_single_domain = session_required_single_domain


class _FakeGare:
    GlobusAuthorizationParameters = _FakeAuthParams


class _FakeConfig:
    def __init__(
        self,
        request_refresh_tokens: bool = False,
        token_validation_error_handler: Any = None,
    ) -> None:
        self.request_refresh_tokens = request_refresh_tokens
        self.token_validation_error_handler = token_validation_error_handler


class _FakeAuthorizer:
    def __init__(self, app: "_FakeUserApp") -> None:
        self._app = app
        self.access_token = "fresh-token"

    def ensure_valid_token(self) -> None:
        # Simulate an expired/invalid token so the SDK invokes the
        # registered token_validation_error_handler, exactly as globus-sdk
        # does when the refresh token needs an interactive re-login.
        handler = self._app.config.token_validation_error_handler
        handler(self._app, RuntimeError("token expired"))


class _FakeUserApp:
    instances: list["_FakeUserApp"] = []

    def __init__(
        self,
        name: str,
        client_id: Any = None,
        scope_requirements: Any = None,
        config: Any = None,
    ) -> None:
        self.config = config
        self.login_calls: list[Any] = []
        _FakeUserApp.instances.append(self)

    def get_authorizer(self, client_id: Any) -> _FakeAuthorizer:
        return _FakeAuthorizer(self)

    def login(self, auth_params: Any = None) -> None:
        self.login_calls.append(auth_params)


class _FakeGlobus:
    UserApp = _FakeUserApp
    GlobusAppConfig = _FakeConfig
    gare = _FakeGare


@pytest.fixture
def fake_globus(monkeypatch) -> type[_FakeGlobus]:
    """Install a fake globus-sdk whose token validation always re-triggers the
    registered handler, and reset the recorded UserApp instances."""
    _FakeUserApp.instances = []
    monkeypatch.setattr(argonne_auth, "_require_globus", lambda: _FakeGlobus)
    return _FakeGlobus


def test_token_paths_include_windows_globus_sdk_store(monkeypatch, tmp_path: Path) -> None:
    """Windows Globus SDK stores app tokens under LOCALAPPDATA."""
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setattr(argonne_auth.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    paths = argonne_auth.token_paths()

    assert str(local_app_data / "globus" / "app") in paths[1]
    assert paths[1].endswith(
        str(Path(argonne_auth.AUTH_CLIENT_ID) / argonne_auth.APP_NAME / "tokens.json")
    )


def test_tokens_exist_checks_windows_globus_sdk_store(monkeypatch, tmp_path: Path) -> None:
    """Status probes should see tokens where Globus SDK actually writes them."""
    local_app_data = tmp_path / "LocalAppData"
    token_path = (
        local_app_data
        / "globus"
        / "app"
        / argonne_auth.AUTH_CLIENT_ID
        / argonne_auth.APP_NAME
        / "tokens.json"
    )
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(argonne_auth.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(argonne_auth, "TOKENS_PATH", str(tmp_path / "missing" / "tokens.json"))

    assert argonne_auth.tokens_exist() is True


def test_token_paths_include_xdg_data_home(monkeypatch, tmp_path: Path) -> None:
    """Linux/macOS probes include XDG-style Globus SDK token storage."""
    xdg_data_home = tmp_path / "xdg"
    monkeypatch.setattr(argonne_auth.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
    monkeypatch.setattr(argonne_auth, "TOKENS_PATH", str(tmp_path / "legacy" / "tokens.json"))

    paths = argonne_auth.token_paths()

    assert str(xdg_data_home / "globus" / "app") in paths[1]


def test_passive_get_access_token_raises_instead_of_login(fake_globus, caplog) -> None:
    """A passive probe (allow_interactive=False) must RAISE login-required rather
    than drive an interactive Globus login when the stored token is invalid."""
    with caplog.at_level("WARNING", logger="clio_agent.providers.argonne_auth"):
        with pytest.raises(argonne_auth.GlobusAuthError):
            argonne_auth.get_access_token(False, allow_interactive=False)

    assert _FakeUserApp.instances, "a UserApp should have been built"
    assert all(app.login_calls == [] for app in _FakeUserApp.instances)
    assert "argonne_login_required" in caplog.text


def test_interactive_get_access_token_drives_login(fake_globus) -> None:
    """The interactive path (default allow_interactive=True) still re-drives the
    Globus login flow through the registered handler."""
    token = argonne_auth.get_access_token(False)

    assert token == "fresh-token"
    assert _FakeUserApp.instances, "a UserApp should have been built"
    assert any(app.login_calls for app in _FakeUserApp.instances)


def test_check_auth_status_never_logs_in(fake_globus, monkeypatch) -> None:
    """check_auth_status is a passive probe: an invalid stored token yields False
    without ever spawning an interactive login."""
    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: True)

    assert argonne_auth.check_auth_status() is False
    assert _FakeUserApp.instances, "a UserApp should have been built"
    assert all(app.login_calls == [] for app in _FakeUserApp.instances)


def test_authenticate_validates_access_token(monkeypatch) -> None:
    """The explicit auth command must prove token usability, not just build an authorizer."""

    calls: list[bool] = []

    def _get_access_token(force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return "token"

    monkeypatch.setattr(argonne_auth, "get_access_token", _get_access_token)

    argonne_auth.authenticate(force=True)

    assert calls == [True]
