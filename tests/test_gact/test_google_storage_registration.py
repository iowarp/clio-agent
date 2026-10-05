"""Shipped Desktop registration works without user-supplied OAuth configuration."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage.auth import StorageAuth, application
from clio_agent.gact.storage.models import SourceRecord
from clio_agent.gact.storage.oauth_clients import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET


@pytest.fixture(autouse=True)
def clear_google_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise distribution defaults independently of the developer environment."""
    for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"CLIO_STORAGE_GOOGLE_{suffix}", raising=False)


@pytest.mark.parametrize("redirect", [None, "http://127.0.0.1:49721/clio-storage-return"])
def test_bundled_google_registration_exchanges_and_refreshes_with_pkce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str | None
) -> None:
    """The actual registered pair reaches only the token endpoint, including refresh."""
    app = application("google_drive")
    assert app.client_id == (
        "882476173721-10t4lm4vskm1l1p98jspmj8pfbequi0g.apps.googleusercontent.com"
    )
    assert app.client_secret
    assert app.client_secret not in repr(app)
    record = StorageAuth.account_login("alice", "clio1", "google_drive")
    auth = StorageAuth(tmp_path / "credentials.json")
    flow = auth.start(record, desktop_redirect=redirect)
    query = parse_qs(urlparse(flow["authorization_url"]).query)
    callback = redirect or "http://127.0.0.1:48173/clio-storage-return"
    assert query["client_id"] == [GOOGLE_CLIENT_ID]
    assert query["redirect_uri"] == [callback]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query
    assert GOOGLE_CLIENT_SECRET not in str(flow)
    assert not auth.connected(record)
    requests: list[dict[str, list[str]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        body = parse_qs(request.content.decode())
        requests.append(body)
        assert body["client_id"] == [GOOGLE_CLIENT_ID]
        assert body["client_secret"] == [GOOGLE_CLIENT_SECRET]
        if body["grant_type"] == ["authorization_code"]:
            assert body["redirect_uri"] == [callback]
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(body["code_verifier"][0].encode()).digest()
            ).rstrip(b"=")
            assert challenge.decode() == query["code_challenge"][0]
            return httpx.Response(
                200,
                json={"access_token": "expired", "refresh_token": "test-refresh", "expires_in": 0},
            )
        assert body["grant_type"] == ["refresh_token"]
        assert body["refresh_token"] == ["test-refresh"]
        return httpx.Response(200, json={"access_token": "renewed", "expires_in": 3600})

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as client:
        auth.complete(
            record,
            flow["flow_id"],
            callback + "?" + urlencode({"state": query["state"][0], "code": "test-code"}),
            client=client,
        )
    original_client = httpx.Client
    monkeypatch.setattr(
        "clio_agent.gact.storage.auth.httpx.Client",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    restarted = StorageAuth(tmp_path / "credentials.json")
    assert restarted.token(record) == "renewed"
    assert len(requests) == 2
    assert GOOGLE_CLIENT_SECRET not in record.model_dump_json()
    assert GOOGLE_CLIENT_SECRET not in (tmp_path / "credentials.json").read_text()


def test_custom_google_registration_never_uses_bundled_client_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overrides stay paired; explicitly empty client IDs still disable sign-in."""
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_CLIENT_ID", "custom-id")
    assert application("google_drive").client_secret == ""
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_CLIENT_SECRET", "custom-value")
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_REDIRECT_URI", "http://127.0.0.1:9999/return")
    app = application("google_drive")
    assert (app.client_id, app.client_secret, app.redirect_uri) == (
        "custom-id",
        "custom-value",
        "http://127.0.0.1:9999/return",
    )
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    assert application("google_drive").client_secret == ""
    monkeypatch.delenv("CLIO_STORAGE_GOOGLE_CLIENT_SECRET")
    assert application("google_drive").client_secret == GOOGLE_CLIENT_SECRET
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_CLIENT_ID", "")
    assert application("google_drive").client_secret == ""
    with pytest.raises(ValueError, match="not been configured"):
        StorageAuth(tmp_path / "credentials.json").start(
            StorageAuth.account_login("alice", "clio1", "google_drive")
        )


@pytest.fixture
def storage_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Serve the production storage routes with an isolated owner data directory."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(tmp_path))}
    register_connected_storage_routes(app)
    with TestClient(app, base_url="http://127.0.0.1:18825") as client:
        yield client
        auth = app.state.connected_storage.auth
        for flow in list(auth._pending.values()):
            auth.disconnect(flow.source_id)


def test_google_account_log_in_http_uses_shipped_registration(storage_client: TestClient) -> None:
    """Provider login is configured out of the box, without creating a source."""
    response = storage_client.get("/v1/storage/providers")
    assert response.status_code == 200
    google = next(row for row in response.json()["providers"] if row["id"] == "google_drive")
    assert google["configured"] is True
    assert google["authenticated"] is False
    assert google["setup_requirement"] is None
    assert GOOGLE_CLIENT_SECRET not in response.text
    started = storage_client.post("/v1/storage/accounts/google_drive/auth/start", json={})
    assert started.status_code == 200
    assert GOOGLE_CLIENT_SECRET not in started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    assert query["client_id"] == [GOOGLE_CLIENT_ID]
    assert storage_client.app.state.connected_storage.store.list("source", SourceRecord) == []


@pytest.mark.parametrize("account", [True, False])
def test_browser_google_return_completes_automatically(
    storage_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    account: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Actual browser callback HTTP -> the existing PKCE exchange -> reusable account."""
    prefix = "/v1/storage/accounts/google_drive"
    if not account:
        created = storage_client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "google_drive",
                "root": "private-folder",
                "label": "Private data",
                "mode": "read_only",
            },
        )
        assert created.status_code == 201, created.text
        prefix = "/v1/workspaces/w/sources/" + created.json()["id"]
    started = storage_client.post(
        prefix + "/auth/start",
        json={},
        headers={
            "Origin": "http://127.0.0.1:4394",
        },
    )
    assert started.status_code == 200, started.text
    flow = started.json()
    assert flow["automatic_callback"] is True
    query = parse_qs(urlparse(flow["authorization_url"]).query)
    redirect = query["redirect_uri"][0]
    assert urlparse(redirect).hostname == "127.0.0.1"
    payload = {"flow_id": flow["flow_id"], "callback_url": ""}
    waiting = storage_client.post(prefix + "/auth/complete", json=payload)
    assert waiting.status_code == 200 and waiting.json()["authenticated"] is False
    with httpx.Client(trust_env=False) as browser:
        rejected = browser.get(redirect, params={"state": "wrong-\u00e9", "code": "not-authorized"})
        assert rejected.status_code == 400
        returned = browser.get(redirect, params={"state": query["state"][0], "code": "test-code"})
        assert returned.status_code == 200
        assert "Return to CLIO" in returned.text
        assert "test-code" not in returned.text
        assert query["state"][0] not in returned.text
        assert returned.headers["cache-control"] == "no-store"
    original_client = httpx.Client

    def exchange(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        body = parse_qs(request.content.decode())
        assert body["redirect_uri"] == [redirect]
        assert body["code"] == ["test-code"]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(body["code_verifier"][0].encode()).digest()
        )
        assert challenge.rstrip(b"=").decode() == query["code_challenge"][0]
        return httpx.Response(200, json={"access_token": "test-access", "expires_in": 3600})

    monkeypatch.setattr(
        "clio_agent.gact.storage.auth.httpx.Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(exchange),
            **kwargs,
        ),
    )
    completed = storage_client.post(prefix + "/auth/complete", json=payload)
    assert completed.status_code == 200 and completed.json()["authenticated"] is True
    assert storage_client.post(prefix + "/auth/complete", json=payload).status_code == 409
    providers = storage_client.get("/v1/storage/providers").json()["providers"]
    assert next(p for p in providers if p["id"] == "google_drive")["authenticated"] is True
    assert "test-access" not in completed.text
    output = capsys.readouterr()
    assert "test-code" not in output.out + output.err


def test_remote_browser_cannot_be_sent_to_unreachable_local_callback(
    storage_client: TestClient,
) -> None:
    response = storage_client.post(
        "/v1/storage/accounts/google_drive/auth/start",
        json={},
        headers={
            "Origin": "https://clio.example.org",
        },
    )
    assert response.status_code == 409
    assert "CLIO Desktop" in response.json()["detail"]


def test_google_desktop_receiver_is_preserved(storage_client: TestClient) -> None:
    redirect = "http://127.0.0.1:49123/clio-storage-return"
    response = storage_client.post(
        "/v1/storage/accounts/google_drive/auth/start",
        json={
            "desktop_redirect": redirect,
        },
        headers={"Origin": "https://remote.example.org"},
    )
    assert response.status_code == 200
    assert response.json()["automatic_callback"] is False
    assert parse_qs(urlparse(response.json()["authorization_url"]).query)["redirect_uri"] == [
        redirect
    ]
