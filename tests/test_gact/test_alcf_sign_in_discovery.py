"""ALCF sign-in -> discovery, end to end on the real objects (owner's 0.9.4.19 bug).

The owner saw "Your ALCF session needs to be verified again", signed in through
Globus, pasted the code -- and nothing changed: the catalog kept reporting
``argonne_reauthentication_required: Token is either not active or invalid``.

These tests drive the REAL routes, the REAL catalog discovery, the REAL
``ArgonneHandshake`` and the REAL ``argonne_auth`` flow. Only the two network
edges are faked: Globus (token exchange + token storage, written to a file the
way the SDK does) and the ALCF gateway (a loopback ``httpx.MockTransport`` that
accepts exactly the token the last sign-in stored and answers anything else
with ALCF's own 401 body, byte-for-byte what the live gateway returns).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.providers import argonne_auth
from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.handshake.argonne import ArgonneHandshake

FIXTURES = Path(__file__).parents[1] / "test_providers" / "fixtures" / "handshake"
_ALCF_REJECTION = {
    "error": {
        "code": "unauthorized",
        "message": "Token is either not active or invalid",
        "info": {},
    }
}


class _Globus:
    """Fake Globus: code exchange + a file-backed token store, like the SDK's."""

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file
        self.authorize_kwargs: list[dict[str, Any]] = []
        self.valid_codes: set[str] = set()

    # -- token storage (what UserApp.token_storage / get_authorizer read) --
    def stored_token(self) -> str | None:
        if not self.token_file.is_file():
            return None
        return json.loads(self.token_file.read_text(encoding="utf-8"))["access_token"]

    def store(self, token: str) -> None:
        self.token_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")

    # -- the globus_sdk surface argonne_auth touches --
    def sdk(self) -> Any:
        globus = self

        class NativeAppAuthClient:
            def __init__(self, client_id: str, *, app_name: str) -> None:
                del client_id, app_name

            def oauth2_start_flow(self, **kwargs: Any) -> None:
                del kwargs

            def oauth2_get_authorize_url(self, **kwargs: Any) -> str:
                globus.authorize_kwargs.append(kwargs)
                return "https://auth.globus.org/v2/oauth2/authorize?state=opaque"

            def oauth2_exchange_code_for_tokens(self, code: str) -> dict[str, str]:
                if code not in globus.valid_codes:
                    raise RuntimeError("invalid_grant")
                return {"access_token": f"token-from-{code}"}

        class _Sdk:
            pass

        _Sdk.NativeAppAuthClient = NativeAppAuthClient  # type: ignore[attr-defined]
        return _Sdk

    def user_app(self, **_kwargs: Any) -> Any:
        globus = self

        class _Authorizer:
            def ensure_valid_token(self) -> None:
                if globus.stored_token() is None:
                    raise RuntimeError("no stored token")

            @property
            def access_token(self) -> str:
                return globus.stored_token() or ""

        class _Storage:
            def store_token_response(self, response: dict[str, str]) -> None:
                globus.store(response["access_token"])

        class _App:
            token_storage = _Storage()

            def get_authorizer(self, resource_server: str) -> _Authorizer:
                assert resource_server == argonne_auth.GATEWAY_CLIENT_ID
                return _Authorizer()

            def logout(self) -> None:
                globus.token_file.unlink(missing_ok=True)

        return _App()


class _Gateway:
    """Loopback ALCF gateway: accepts only the token the last sign-in stored."""

    def __init__(self, globus: _Globus) -> None:
        self.globus = globus
        self.bearers: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "")
        if request.url.path.endswith(("/models", "/jobs")):
            self.bearers.append(bearer)
        valid = self.globus.stored_token()
        if not valid or valid.startswith("revoked") or bearer != f"Bearer {valid}":
            return httpx.Response(401, json=_ALCF_REJECTION)
        if request.url.path.endswith("/models"):
            payload = json.loads((FIXTURES / "alcf_metis_models.json").read_text())
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"running": []})


@pytest.fixture
def alcf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, globus_sdk_installed: Any
) -> tuple[_Globus, _Gateway]:
    del globus_sdk_installed
    for var in ("CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN", "CLIO_LM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    globus = _Globus(tmp_path / "tokens.json")
    gateway = _Gateway(globus)
    monkeypatch.setattr(argonne_auth, "token_paths", lambda: (str(globus.token_file),))
    monkeypatch.setattr(argonne_auth, "_require_globus", globus.sdk)
    monkeypatch.setattr(argonne_auth, "_build_user_app", globus.user_app)
    monkeypatch.setattr(
        "clio_agent.gact.routes.provider_auth.ensure_argonne_support", lambda: False
    )

    async def _open_client(self: ArgonneHandshake, ctx: Any) -> httpx.AsyncClient:
        del self, ctx
        return httpx.AsyncClient(transport=httpx.MockTransport(gateway.handler))

    monkeypatch.setattr(ArgonneHandshake, "_open_client", _open_client)
    argonne_auth._pending_authentications.clear()
    argonne_auth._finished_authentications.clear()
    handshake_cache.invalidate_provider("argonne_metis")
    return globus, gateway


def _metis(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/provider-catalog?provider=argonne_metis")
    assert response.status_code == 200
    return next(p for p in response.json()["providers"] if p["id"] == "argonne_metis")


def _auth(client: TestClient, body: dict[str, Any]) -> httpx.Response:
    return client.post("/v1/providers/argonne_metis/auth", json=body)


def test_sign_in_replaces_a_rejected_token_and_discovery_uses_the_new_one(
    tmp_path: Path, alcf: tuple[_Globus, _Gateway]
) -> None:
    """The owner's exact sequence: rejected -> Sign in again -> paste -> models."""

    globus, gateway = alcf
    globus.store("revoked-token")
    globus.valid_codes.add("code-1")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        before = _metis(client)
        assert before["auth"] == "rejected"
        assert before["failure"].startswith("argonne_reauthentication_required")

        started = _auth(client, {"action": "start", "force": True})
        assert started.status_code == 200
        flow_id = started.json()["flow_id"]
        assert globus.authorize_kwargs[-1]["prompt"] == "login"
        # ALCF's own session policy, as its official auth helper requests it.
        assert globus.authorize_kwargs[-1]["session_required_policies"] == [
            "83732ff2-9c42-4548-b5ce-17e498c84f6a"
        ]
        assert _auth(client, {"action": "status", "flow_id": flow_id}).json()["state"] == "pending"

        done = _auth(
            client, {"action": "complete", "flow_id": flow_id, "authorization_code": "code-1"}
        )
        assert done.status_code == 200
        status = _auth(client, {"action": "status", "flow_id": flow_id}).json()
        assert status == {"provider_id": "argonne_metis", "state": "complete", "reason": ""}

        gateway.bearers.clear()
        after = _metis(client)

    # Discovery sent exactly the token the sign-in just stored -- never a
    # placeholder, never the old revoked one.
    assert gateway.bearers
    assert set(gateway.bearers) == {"Bearer token-from-code-1"}
    assert after["auth"] == "ok"
    assert after["health"] == "ready"
    assert after["failure"] == ""
    assert after["models"]
    assert "gpt-oss-120b" in json.dumps(after["models"])


def test_discovery_never_sends_a_placeholder_bearer_to_alcf(
    tmp_path: Path, alcf: tuple[_Globus, _Gateway]
) -> None:
    """A valid stored sign-in is used as-is: the keyless ``EMPTY`` probe key
    (for local OpenAI-compatible servers) must never reach ALCF's gateway."""

    globus, gateway = alcf
    globus.store("good-token")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        metis = _metis(client)
        handshake = client.get("/v1/providers/argonne_metis/handshake?refresh=true")

    assert "Bearer EMPTY" not in gateway.bearers
    assert set(gateway.bearers) == {"Bearer good-token"}
    assert metis["health"] == "ready"
    assert handshake.status_code == 200
    assert handshake.json()["auth"] == "ok"


def test_sign_in_again_after_a_completed_flow_starts_a_fresh_forced_login(
    tmp_path: Path, alcf: tuple[_Globus, _Gateway]
) -> None:
    """A finished flow never blocks the next click: every start is a new,
    forced (``prompt=login``) Globus flow with its own id."""

    globus, _gateway = alcf
    globus.valid_codes.add("code-1")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        first = _auth(client, {"action": "start", "force": True}).json()["flow_id"]
        _auth(client, {"action": "complete", "flow_id": first, "authorization_code": "code-1"})
        second = _auth(client, {"action": "start", "force": True})
        second_status = _auth(client, {"action": "status", "flow_id": second.json()["flow_id"]})

    assert second.status_code == 200
    assert second.json()["flow_id"] != first
    assert [kwargs.get("prompt") for kwargs in globus.authorize_kwargs] == ["login", "login"]
    assert second_status.json()["state"] == "pending"


def test_status_of_an_unknown_flow_is_failed_with_a_plain_reason(
    tmp_path: Path, alcf: tuple[_Globus, _Gateway]
) -> None:
    """``status`` never claims ``complete`` for a flow it never finished."""

    del alcf
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        unknown = _auth(client, {"action": "status", "flow_id": "never-started"}).json()
        empty = _auth(client, {"action": "status"}).json()

    for status in (unknown, empty):
        assert status["state"] == "failed"
        assert "Start sign-in again" in status["reason"]


def test_a_rejected_code_keeps_the_flow_open_and_says_why(
    tmp_path: Path, alcf: tuple[_Globus, _Gateway]
) -> None:
    """A mistyped code is a plain error and the same flow accepts a retry --
    status never flips to ``complete`` on a failed exchange."""

    globus, _gateway = alcf
    globus.valid_codes.add("right-code")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        flow_id = _auth(client, {"action": "start", "force": True}).json()["flow_id"]
        bad = _auth(
            client, {"action": "complete", "flow_id": flow_id, "authorization_code": "typo"}
        )
        status_after_bad = _auth(client, {"action": "status", "flow_id": flow_id}).json()
        good = _auth(
            client, {"action": "complete", "flow_id": flow_id, "authorization_code": "right-code"}
        )

    assert bad.status_code == 502
    assert "Could not complete ALCF sign-in" in bad.text
    assert status_after_bad["state"] == "pending"
    assert good.status_code == 200
    assert globus.stored_token() == "token-from-right-code"
