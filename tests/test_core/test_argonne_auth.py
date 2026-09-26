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

    def logout(self) -> None:
        self.logout_calls = getattr(self, "logout_calls", 0) + 1


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


def test_browser_auth_flow_exchanges_code_and_stores_tokens_on_agent(monkeypatch) -> None:
    """The desktop gets only a URL/id; the agent exchanges and stores the token."""

    class _NativeClient:
        instances: list["_NativeClient"] = []

        def __init__(self, client_id: str, *, app_name: str) -> None:
            self.client_id = client_id
            self.app_name = app_name
            self.start_kwargs: dict[str, Any] = {}
            self.url_kwargs: dict[str, Any] = {}
            self.codes: list[str] = []
            self.instances.append(self)

        def oauth2_start_flow(self, **kwargs: Any) -> None:
            self.start_kwargs = kwargs

        def oauth2_get_authorize_url(self, **kwargs: Any) -> str:
            self.url_kwargs = kwargs
            return "https://auth.globus.org/v2/oauth2/authorize?state=opaque"

        def oauth2_exchange_code_for_tokens(self, code: str) -> object:
            self.codes.append(code)
            return {"token": "response"}

    class _BrowserGlobus:
        NativeAppAuthClient = _NativeClient

    class _Storage:
        def __init__(self) -> None:
            self.responses: list[object] = []

        def store_token_response(self, response: object) -> None:
            self.responses.append(response)

    class _ValidAuthorizer:
        def __init__(self) -> None:
            self.validated = False

        def ensure_valid_token(self) -> None:
            self.validated = True

    storage = _Storage()
    authorizer = _ValidAuthorizer()

    class _StorageApp:
        token_storage = storage

        def get_authorizer(self, resource_server: str) -> _ValidAuthorizer:
            assert resource_server == argonne_auth.GATEWAY_CLIENT_ID
            return authorizer

    monkeypatch.setattr(argonne_auth, "_require_globus", lambda: _BrowserGlobus)
    monkeypatch.setattr(argonne_auth, "_build_user_app", lambda **kwargs: _StorageApp())
    argonne_auth._pending_authentications.clear()

    pending = argonne_auth.begin_authentication()

    assert pending.authorization_url.startswith("https://auth.globus.org/")
    client = _NativeClient.instances[-1]
    assert client.start_kwargs["requested_scopes"] == [argonne_auth.GATEWAY_SCOPE, "openid"]
    assert client.start_kwargs["refresh_tokens"] is True
    assert client.url_kwargs["session_required_single_domain"] == argonne_auth.ALLOWED_DOMAINS
    assert "prompt" not in client.url_kwargs

    argonne_auth.complete_authentication(pending.flow_id, "  one-time-code  ")

    assert client.codes == ["one-time-code"]
    assert storage.responses == [{"token": "response"}]
    assert authorizer.validated is True
    assert pending.flow_id not in argonne_auth._pending_authentications


def test_browser_auth_rejects_unknown_or_expired_flow() -> None:
    """An authorization code cannot be applied without its agent-held PKCE flow."""

    argonne_auth._pending_authentications.clear()

    with pytest.raises(argonne_auth.GlobusAuthError, match="expired"):
        argonne_auth.complete_authentication("missing", "one-time-code")


def test_force_login_sets_globus_auths_own_prompt_login_param(monkeypatch) -> None:
    """'Sign in again' (argonne_reauthentication_required) forces a fresh
    Globus login via the SDK's real ``prompt='login'`` parameter (verified
    live against the installed globus-sdk 4.6 API) -- never reusing a
    browser session that could reproduce the same rejected credential."""

    class _NativeClient:
        instances: list["_NativeClient"] = []

        def __init__(self, client_id: str, *, app_name: str) -> None:
            del client_id, app_name
            self.url_kwargs: dict[str, Any] = {}
            _NativeClient.instances.append(self)

        def oauth2_start_flow(self, **kwargs: Any) -> None:
            del kwargs

        def oauth2_get_authorize_url(self, **kwargs: Any) -> str:
            self.url_kwargs = kwargs
            return "https://auth.globus.org/v2/oauth2/authorize?state=opaque"

    class _ForceLoginGlobus:
        NativeAppAuthClient = _NativeClient

    monkeypatch.setattr(argonne_auth, "_require_globus", lambda: _ForceLoginGlobus)
    argonne_auth._pending_authentications.clear()

    argonne_auth.begin_authentication(force_login=True)

    assert _NativeClient.instances[-1].url_kwargs["prompt"] == "login"


class TestSignOut:
    """The owner's live-tested defect: 'Sign out' did nothing for ALCF. Now it
    revokes and deletes every stored Globus token through the SDK's own
    documented ``UserApp.logout()`` -- and is idempotent."""

    def _write_token_file(self, monkeypatch, tmp_path: Path) -> Path:
        token_file = tmp_path / "tokens.json"
        token_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(argonne_auth, "token_paths", lambda: (str(token_file),))
        return token_file

    def test_revokes_through_user_app_and_deletes_the_stored_file(
        self, fake_globus, monkeypatch, tmp_path: Path
    ) -> None:
        token_file = self._write_token_file(monkeypatch, tmp_path)

        argonne_auth.sign_out()

        assert _FakeUserApp.instances, "a UserApp should have been built"
        assert getattr(_FakeUserApp.instances[-1], "logout_calls", 0) == 1
        assert not token_file.exists()

    def test_a_second_sign_out_is_a_noop(self, fake_globus, monkeypatch, tmp_path: Path) -> None:
        self._write_token_file(monkeypatch, tmp_path)

        argonne_auth.sign_out()
        argonne_auth.sign_out()  # nothing stored now -- must not raise

    def test_still_deletes_the_local_file_when_revocation_fails(
        self, fake_globus, monkeypatch, tmp_path: Path
    ) -> None:
        token_file = self._write_token_file(monkeypatch, tmp_path)

        def _failing_logout(self: Any) -> None:
            raise RuntimeError("Globus unreachable")

        monkeypatch.setattr(_FakeUserApp, "logout", _failing_logout)

        argonne_auth.sign_out()  # must not raise -- deletion still proceeds

        assert not token_file.exists()

    def test_deletes_the_local_file_even_without_the_argonne_extra(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        token_file = self._write_token_file(monkeypatch, tmp_path)

        def _unavailable(**_kwargs: Any) -> None:
            raise argonne_auth.GlobusUnavailable("missing")

        monkeypatch.setattr(argonne_auth, "_build_user_app", _unavailable)

        argonne_auth.sign_out()  # no client to revoke through, but the file still goes

        assert not token_file.exists()
