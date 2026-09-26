"""ALCF paste-code sign-in, end to end through ``POST /v1/providers/{id}/auth``.

Drives the real route, the real :mod:`clio_agent.providers.argonne_auth`
flow registry and its real on-disk token handling. Only the Globus network
boundary (``globus_sdk``) is faked: a code exchange that returns a token
response, a ``UserApp`` whose token storage writes the real token file, and a
``logout`` that records the revocation.

The path under test is the one the model picker drives: Sign in (start) ->
paste the Globus code (complete) -> the preset reads signed in and offers
Sign out -> Sign out revokes through the SDK and deletes the token file.
"""

from __future__ import annotations

import importlib.machinery
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.providers import argonne_auth

_PROVIDER_ID = "argonne_sophia"
_GOOD_CODE = "globus-code-123"


class _FakeGlobus:
    """The slice of ``globus_sdk`` the ALCF flow touches, backed by one token file."""

    def __init__(self, tokens_path: Path) -> None:
        self.tokens_path = tokens_path
        self.revoked: list[str] = []
        self.authorize_kwargs: list[dict[str, Any]] = []
        fake = self

        class NativeAppAuthClient:
            def __init__(self, client_id: str, app_name: str) -> None:
                del client_id, app_name

            def oauth2_start_flow(self, **kwargs: Any) -> None:
                del kwargs

            def oauth2_get_authorize_url(self, **kwargs: Any) -> str:
                fake.authorize_kwargs.append(kwargs)
                return "https://auth.globus.org/v2/oauth2/authorize?fake=1"

            def oauth2_exchange_code_for_tokens(self, code: str) -> dict[str, str]:
                if code != _GOOD_CODE:
                    raise RuntimeError("invalid_grant")
                return {"access_token": "access-abc", "refresh_token": "refresh-abc"}

        class _TokenStorage:
            def store_token_response(self, response: dict[str, str]) -> None:
                fake.tokens_path.parent.mkdir(parents=True, exist_ok=True)
                fake.tokens_path.write_text(json.dumps(response), encoding="utf-8")

        class _Authorizer:
            access_token = "access-abc"

            def ensure_valid_token(self) -> None:
                if not fake.tokens_path.is_file():
                    raise RuntimeError("no stored token")

        class UserApp:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs
                self.token_storage = _TokenStorage()

            def get_authorizer(self, resource_server: str) -> _Authorizer:
                del resource_server
                return _Authorizer()

            def login(self, **kwargs: Any) -> None:  # pragma: no cover - never interactive here
                raise AssertionError("the paste-code flow must never run an interactive login")

            def logout(self) -> None:
                if fake.tokens_path.is_file():
                    token = json.loads(fake.tokens_path.read_text(encoding="utf-8"))
                    fake.revoked.extend([token["access_token"], token["refresh_token"]])

        module = types.ModuleType("globus_sdk")
        module.__spec__ = importlib.machinery.ModuleSpec("globus_sdk", loader=None)
        module.NativeAppAuthClient = NativeAppAuthClient  # type: ignore[attr-defined]
        module.UserApp = UserApp  # type: ignore[attr-defined]
        module.GlobusAppConfig = lambda **kwargs: kwargs  # type: ignore[attr-defined]
        self.module = module


@pytest.fixture
def globus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeGlobus:
    tokens_path = tmp_path / "globus" / "tokens.json"
    fake = _FakeGlobus(tokens_path)
    monkeypatch.setitem(sys.modules, "globus_sdk", fake.module)
    monkeypatch.setattr(argonne_auth, "token_paths", lambda: (str(tokens_path),))
    monkeypatch.delenv("CLIO_ARGONNE_TOKEN", raising=False)
    monkeypatch.delenv("ALCF_INFERENCE_TOKEN", raising=False)
    # Support is "already installed" (the fake module above); never pip-install.
    monkeypatch.setattr(
        "clio_agent.gact.routes.provider_auth.ensure_argonne_support", lambda: False
    )
    return fake


def _auth(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(f"/v1/providers/{_PROVIDER_ID}/auth", json=body)


def test_paste_code_sign_in_then_sign_out_revokes_and_deletes_tokens(
    tmp_path: Path, globus: _FakeGlobus
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        assert argonne_auth.readiness()[0] == "auth_required"

        started = _auth(client, {"action": "start", "force": True})
        assert started.status_code == 200, started.text
        start = started.json()
        assert start["browser"]["authorization_url"].startswith("https://auth.globus.org/")
        # An explicit Sign in click forces a fresh Globus login.
        assert globus.authorize_kwargs[-1]["prompt"] == "login"
        assert (
            _auth(client, {"action": "status", "flow_id": start["flow_id"]}).json()["state"]
            == "pending"
        )

        completed = _auth(
            client,
            {"action": "complete", "flow_id": start["flow_id"], "paste": f"  {_GOOD_CODE}\n"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["is_authenticated"] is True
        assert globus.tokens_path.is_file()
        assert argonne_auth.readiness() == ("ready", "Globus token validated", True)
        # The flow is consumed: a second paste of the same flow cannot reuse it.
        assert (
            _auth(client, {"action": "status", "flow_id": start["flow_id"]}).json()["state"]
            == "complete"
        )

        signed_out = _auth(client, {"action": "logout"})
        assert signed_out.status_code == 200, signed_out.text
        assert signed_out.json() == {
            "provider_id": _PROVIDER_ID,
            "is_authenticated": False,
            "instructions": "Signed out of ALCF.",
        }
        assert globus.revoked == ["access-abc", "refresh-abc"]
        assert not globus.tokens_path.exists()
        assert argonne_auth.readiness()[0] == "auth_required"


def test_a_wrong_pasted_code_is_a_typed_error_and_stores_nothing(
    tmp_path: Path, globus: _FakeGlobus
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        start = _auth(client, {"action": "start"}).json()
        failed = _auth(client, {"action": "complete", "flow_id": start["flow_id"], "paste": "nope"})

    assert failed.status_code == 502
    assert failed.json()["error"]["error"] == "argonne_auth_failed"
    assert not globus.tokens_path.exists()
    assert argonne_auth.readiness()[0] == "auth_required"
