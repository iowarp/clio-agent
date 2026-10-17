"""NDP device-PKCE protocol, private grant ownership and restart/refresh behavior."""

from __future__ import annotations

import base64
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs

import httpx
import pytest

from clio_agent.gact.storage import ndp_oauth
from clio_agent.gact.storage.auth import StorageAuth, application

ISSUER = "https://ndp.test/realms/NDP"


@pytest.fixture
def protocol(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, object]]:
    """Replace only the HTTP boundary; exercise real discovery, device and auth owners."""
    monkeypatch.setenv("CLIO_STORAGE_NDP_ISSUER", ISSUER)
    monkeypatch.setenv("CLIO_STORAGE_NDP_CLIENT_ID", "cilo")
    state: dict[str, object] = {"requests": [], "error": "authorization_pending", "refreshes": 0}
    original = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        requests = state["requests"]
        assert isinstance(requests, list)
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "device_authorization_endpoint": ISSUER + "/device",
                    "token_endpoint": (
                        "https://evil.test/token"
                        if state.get("evil_discovery")
                        else ISSUER + "/token"
                    ),
                    "revocation_endpoint": ISSUER + "/revoke",
                    "userinfo_endpoint": ISSUER + "/userinfo",
                },
            )
        if request.url.path.endswith("/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": ISSUER + "/verify",
                    "verification_uri_complete": ISSUER + "/verify?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/token"):
            payload = parse_qs(request.content.decode())
            if payload["grant_type"] == ["refresh_token"]:
                state["refreshes"] = int(state["refreshes"]) + 1  # type: ignore[arg-type]
                assert payload["refresh_token"] == ["private-refresh"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "new-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 3600,
                    },
                )
            if state["error"]:
                return httpx.Response(400, json={"error": state["error"]})
            return httpx.Response(
                200,
                json={
                    "access_token": "private-access",
                    "refresh_token": "private-refresh",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/userinfo"):
            if state.get("profile_down"):
                raise httpx.ConnectError("unavailable", request=request)
            return httpx.Response(200, json={"preferred_username": "alice"})
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        raise AssertionError(request.url)

    def client(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(respond)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", client)
    ndp_oauth.discovery.cache_clear()
    yield state
    ndp_oauth.discovery.cache_clear()


def test_device_pkce_refresh_restart_and_signout(
    tmp_path: Path, protocol: dict[str, object]
) -> None:
    """Use the registered public flow, persist/rotate grants and revoke across workspaces."""
    auth = StorageAuth(tmp_path / "auth.json")
    record = auth.account_login("alice", "clio", "ndp")
    started = auth.start(record)
    assert started["authorization_url"].endswith("verify?user_code=ABCD-EFGH")
    assert "private-device" not in str(started)
    flow = auth._pending[started["flow_id"]]
    requests = protocol["requests"]
    assert isinstance(requests, list)
    payload = parse_qs(requests[-1].content.decode())
    assert payload["scope"] == ["openid offline_access"]
    assert payload["client_id"] == ["cilo"]
    assert payload["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    ]
    assert not auth.complete(record, started["flow_id"], "")
    assert not auth.complete(record, started["flow_id"], "")
    assert len([r for r in requests if r.url.path.endswith("/token")]) == 1
    assert flow.device is not None
    flow.device.next_poll_at = 0
    protocol["error"] = "slow_down"
    assert not auth.complete(record, started["flow_id"], "")
    assert flow.device.interval == 10
    flow.device.next_poll_at = 0
    protocol["error"] = ""
    assert auth.complete(record, started["flow_id"], "")
    assert auth.account_name("alice", "clio", "ndp") == "alice"
    auth = StorageAuth(tmp_path / "auth.json")
    assert auth.connected(record)
    entries = auth._entries()
    account = next(row for row in entries.values() if row.get("kind") == "account")
    tokens = account["tokens"]
    assert isinstance(tokens, dict)
    tokens["expires_at"] = 0
    auth._vault.write_entries(entries)
    with ThreadPoolExecutor(max_workers=5) as executor:
        assert list(executor.map(lambda _: auth.token(record), range(5))) == ["new-access"] * 5
    assert protocol["refreshes"] == 1
    assert StorageAuth(tmp_path / "auth.json").token(record) == "new-access"
    auth.sign_out("alice", "clio", "ndp")
    assert not auth.connected(record)
    assert "private-refresh" not in (tmp_path / "auth.json").read_text()
    revoked = [r for r in requests if r.url.path.endswith("/revoke")]
    assert len(revoked) == 1
    assert parse_qs(revoked[0].content.decode())["token"] == ["rotated-refresh"]


@pytest.mark.parametrize("error", ["access_denied", "expired_token", "invalid_grant"])
def test_terminal_device_errors_clear_pending(
    tmp_path: Path, protocol: dict[str, object], error: str
) -> None:
    auth = StorageAuth(tmp_path / "auth.json")
    record = auth.account_login("alice", "clio", "ndp")
    flow = auth.start(record)
    protocol["error"] = error
    with pytest.raises(ValueError):
        auth.complete(record, flow["flow_id"], "")
    assert not auth._pending
    assert not auth.connected(record)


def test_cancel_owner_expiry_and_profile_outage(
    tmp_path: Path, protocol: dict[str, object]
) -> None:
    auth = StorageAuth(tmp_path / "auth.json")
    record = auth.account_login("alice", "clio", "ndp")
    flow = auth.start(record)
    auth.cancel(auth.account_login("another", "clio", "ndp"), flow["flow_id"])
    assert auth._pending
    auth.cancel(record, flow["flow_id"])
    assert not auth._pending
    flow = auth.start(record)
    auth._pending[flow["flow_id"]].expires_at = time.time() - 1
    with pytest.raises(ValueError, match="expired"):
        auth.complete(record, flow["flow_id"], "")
    flow = auth.start(record)
    protocol.update(error="", profile_down=True)
    assert auth.complete(record, flow["flow_id"], "")
    assert auth.connected(record)


def test_realm_change_cannot_reuse_a_grant(
    tmp_path: Path, protocol: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = StorageAuth(tmp_path / "auth.json")
    record = auth.account_login("alice", "clio", "ndp")
    flow = auth.start(record)
    protocol["error"] = ""
    auth.complete(record, flow["flow_id"], "")
    monkeypatch.setenv("CLIO_STORAGE_NDP_CLIENT_ID", "production-client")
    assert not auth.connected(record)
    assert not auth.account_connected("alice", "clio", "ndp")
    with pytest.raises(PermissionError):
        auth.token(record)


def test_discovery_rejects_cross_origin_credentials(
    protocol: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    ndp_oauth.discovery.cache_clear()
    protocol["evil_discovery"] = True
    with pytest.raises(ValueError, match="another host"):
        application("ndp")
    protocol["evil_discovery"] = False
    ndp_oauth.discovery.cache_clear()
    # The HTTPS validator also protects verification and Endpoint registration.
    with pytest.raises(ValueError, match="HTTPS"):
        ndp_oauth.https_url("http://ndp.test/token")
    assert application("ndp").client_secret == ""
