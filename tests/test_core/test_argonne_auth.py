"""Tests for Argonne / ALCF auth helper behavior."""

from __future__ import annotations

from pathlib import Path

from clio_agent.providers import argonne_auth


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


def test_authenticate_validates_access_token(monkeypatch) -> None:
    """The explicit auth command must prove token usability, not just build an authorizer."""

    calls: list[bool] = []

    def _get_access_token(force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return "token"

    monkeypatch.setattr(argonne_auth, "get_access_token", _get_access_token)

    argonne_auth.authenticate(force=True)

    assert calls == [True]
