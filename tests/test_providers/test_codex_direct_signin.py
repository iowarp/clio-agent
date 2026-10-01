"""Codex direct sign-in: CLIO's own credential, else the local Codex CLI login.

The direct transport is usable when either exists (``direct_signed_in``); the CLI
login is read, never refreshed by CLIO (lm15 owns that, under the CLI's own lock), and
every availability check (bind readiness, catalog handshake, discovery, doctor) asks the
same question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.providers.codex.credentials import (
    CodexCredentialStore,
    codex_cli_auth_path,
    codex_cli_signed_in,
    direct_auth_headers,
    direct_signed_in,
)
from clio_agent.providers.codex.errors import CodexCredentialMissingError


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def no_clio_signin(tmp_path: Path) -> CodexCredentialStore:
    return CodexCredentialStore(path=tmp_path / "clio" / "codex.json")


def _write_cli_login(home: Path, **tokens: str) -> None:
    (home / "auth.json").write_text(json.dumps({"tokens": tokens}), encoding="utf-8")


def test_the_cli_login_path_follows_codex_home(codex_home: Path) -> None:
    assert codex_cli_auth_path() == codex_home / "auth.json"


def test_no_login_anywhere_is_signed_out(
    codex_home: Path, no_clio_signin: CodexCredentialStore
) -> None:
    assert not codex_cli_signed_in()
    assert not direct_signed_in(no_clio_signin)
    with pytest.raises(CodexCredentialMissingError):
        direct_auth_headers(no_clio_signin)


def test_a_cli_login_with_token_and_account_signs_direct_in(
    codex_home: Path, no_clio_signin: CodexCredentialStore
) -> None:
    _write_cli_login(codex_home, access_token="tok", account_id="acct", refresh_token="r")
    assert codex_cli_signed_in()
    assert direct_signed_in(no_clio_signin)


@pytest.mark.parametrize("tokens", [{"access_token": "tok"}, {"account_id": "acct"}, {}])
def test_an_incomplete_cli_login_is_not_a_sign_in(
    codex_home: Path, no_clio_signin: CodexCredentialStore, tokens: dict[str, str]
) -> None:
    _write_cli_login(codex_home, **tokens)
    assert not direct_signed_in(no_clio_signin)


def test_an_unreadable_cli_login_is_not_a_sign_in(
    codex_home: Path, no_clio_signin: CodexCredentialStore
) -> None:
    (codex_home / "auth.json").write_text("{not json", encoding="utf-8")
    assert not codex_cli_signed_in()


def test_clio_signin_wins_and_is_refreshed_by_clio(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Signed:
        def is_signed_in(self) -> bool:
            return True

        def get_valid_credential(self) -> object:
            return type("C", (), {"access_token": "clio-tok", "account_id": "clio-acct"})()

    _write_cli_login(codex_home, access_token="cli-tok", account_id="cli-acct")
    headers = direct_auth_headers(Signed())  # type: ignore[arg-type]
    assert headers == {"Authorization": "Bearer clio-tok", "chatgpt-account-id": "clio-acct"}


def test_the_cli_login_headers_come_from_lm15_never_a_clio_refresh(
    codex_home: Path, no_clio_signin: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lm15 reads the login at ``$CODEX_HOME/auth.json`` -- not its own fixed ``~/.codex``."""
    _write_cli_login(codex_home, access_token="cli-tok", account_id="cli-acct")
    refreshed: list[bool] = []
    monkeypatch.setattr(
        CodexCredentialStore, "get_valid_credential", lambda self: refreshed.append(True)
    )
    assert direct_auth_headers(no_clio_signin) == {
        "Authorization": "Bearer cli-tok",
        "chatgpt-account-id": "cli-acct",
    }
    assert refreshed == []
