"""Unit tests for the durable Codex credential store (A.4, A.9)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import (
    CodexCredentialMissingError,
    CodexRefreshFailedError,
)
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.oauth import OAuthError, TokenResponse


def _credential(*, expires_in_ms: int = 60 * 60 * 1000) -> CodexCredential:
    return CodexCredential(
        access_token="at",
        refresh_token="rt",
        expires_at_ms=int(time.time() * 1000) + expires_in_ms,
        account_id="acct_1",
    )


def test_load_returns_none_when_never_signed_in(tmp_path: Path) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    assert store.load() is None
    assert store.is_signed_in() is False


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    credential = _credential()
    store.save(credential)
    loaded = store.load()
    assert loaded == credential
    assert store.is_signed_in() is True


def test_save_persists_at_0600(tmp_path: Path) -> None:
    path = tmp_path / "codex_credential.json"
    store = CodexCredentialStore(path=path)
    store.save(_credential())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "clio-agent.codex-credential.v1"
    assert "default" in payload["entries"]


def test_logout_deletes_credential(tmp_path: Path) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    store.save(_credential())
    assert store.is_signed_in() is True
    store.logout()
    assert store.is_signed_in() is False


def test_logout_when_never_signed_in_is_a_noop(tmp_path: Path) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    store.logout()  # must not raise
    assert store.is_signed_in() is False


def test_get_valid_credential_raises_when_missing(tmp_path: Path) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    with pytest.raises(CodexCredentialMissingError):
        store.get_valid_credential()


def test_get_valid_credential_returns_fresh_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    store.save(_credential(expires_in_ms=60 * 60 * 1000))

    def _fail_refresh(*_a: object, **_k: object) -> TokenResponse:
        raise AssertionError("refresh must not be called for a fresh credential")

    monkeypatch.setattr(
        "clio_agent.providers.codex.credentials._refresh_token_request", _fail_refresh
    )
    result = store.get_valid_credential()
    assert result.access_token == "at"


def test_get_valid_credential_refreshes_when_near_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    # Under REFRESH_MARGIN_MS remaining -> proactive refresh (A.4).
    store.save(_credential(expires_in_ms=int(c.REFRESH_MARGIN_MS / 2)))

    def _refresh(refresh_token_value: str) -> TokenResponse:
        assert refresh_token_value == "rt"
        return TokenResponse(access_token="new_at", refresh_token="new_rt", expires_in=3600)

    monkeypatch.setattr("clio_agent.providers.codex.credentials._refresh_token_request", _refresh)
    monkeypatch.setattr(
        "clio_agent.providers.codex.credentials.decode_account_id", lambda _token: "acct_1"
    )
    result = store.get_valid_credential()
    assert result.access_token == "new_at"
    assert result.refresh_token == "new_rt"
    # Refresh rotation is persisted atomically (A.4): a fresh store reload sees it.
    reloaded = CodexCredentialStore(path=tmp_path / "codex_credential.json").load()
    assert reloaded is not None
    assert reloaded.access_token == "new_at"
    assert reloaded.refresh_token == "new_rt"


def test_get_valid_credential_force_refresh_even_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    store.save(_credential(expires_in_ms=60 * 60 * 1000))
    calls = {"n": 0}

    def _refresh(refresh_token_value: str) -> TokenResponse:
        calls["n"] += 1
        return TokenResponse(access_token="forced_at", refresh_token="forced_rt", expires_in=3600)

    monkeypatch.setattr("clio_agent.providers.codex.credentials._refresh_token_request", _refresh)
    monkeypatch.setattr(
        "clio_agent.providers.codex.credentials.decode_account_id", lambda _token: "acct_1"
    )
    result = store.get_valid_credential(force_refresh=True)
    assert result.access_token == "forced_at"
    assert calls["n"] == 1


def test_refresh_failure_raises_typed_error_and_does_not_corrupt_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "codex_credential.json"
    store = CodexCredentialStore(path=path)
    original = _credential(expires_in_ms=int(c.REFRESH_MARGIN_MS / 2))
    store.save(original)

    def _fail(*_a: object, **_k: object) -> TokenResponse:
        raise OAuthError("refresh token rejected")

    monkeypatch.setattr("clio_agent.providers.codex.credentials._refresh_token_request", _fail)
    with pytest.raises(CodexRefreshFailedError):
        store.get_valid_credential()
    # The stale credential is left untouched -- a failed refresh must not corrupt it.
    assert store.load() == original


def test_mark_invalid_after_401_forces_a_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CodexCredentialStore(path=tmp_path / "codex_credential.json")
    store.save(_credential(expires_in_ms=60 * 60 * 1000))

    def _refresh(refresh_token_value: str) -> TokenResponse:
        return TokenResponse(access_token="after_401", refresh_token="rt2", expires_in=3600)

    monkeypatch.setattr("clio_agent.providers.codex.credentials._refresh_token_request", _refresh)
    monkeypatch.setattr(
        "clio_agent.providers.codex.credentials.decode_account_id", lambda _token: "acct_1"
    )
    result = store.mark_invalid_after_401()
    assert result.access_token == "after_401"
