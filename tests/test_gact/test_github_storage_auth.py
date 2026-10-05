"""Exercise GitHub device grants, shared ownership and real authenticated fsspec reads."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage import github_oauth
from clio_agent.gact.storage.auth import StorageAuth, application
from clio_agent.gact.storage.linked import link_folder, linked_file
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.service import StorageService


def test_distributed_github_identity_and_custom_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIO_STORAGE_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("CLIO_STORAGE_GITHUB_APP_URL", raising=False)
    monkeypatch.delenv("CLIO_STORAGE_GITHUB_CLIENT_SECRET", raising=False)
    assert application("github").client_id == "Iv23lieBkZ0yxGbLAIyE"
    assert not application("github").client_secret
    assert github_oauth.account_url() == "https://github.com/apps/clio-agent/installations/new"
    monkeypatch.setenv("CLIO_STORAGE_GITHUB_CLIENT_ID", "custom-app")
    assert github_oauth.account_url() == "https://github.com/settings/installations"
    monkeypatch.setenv("CLIO_STORAGE_GITHUB_APP_URL", "https://github.com/apps/custom-app")
    assert github_oauth.account_url() == "https://github.com/apps/custom-app/installations/new"
    monkeypatch.setenv("CLIO_STORAGE_GITHUB_APP_URL", "https://untrusted.example/apps/custom-app")
    assert github_oauth.account_url() == "https://github.com/settings/installations"


@pytest.fixture
def github_login(monkeypatch: pytest.MonkeyPatch) -> tuple[list[httpx.Request], list[float]]:
    calls: list[httpx.Request] = []
    now = [1000.0]
    monkeypatch.setenv("CLIO_STORAGE_GITHUB_CLIENT_ID", "test-client")
    monkeypatch.setattr(github_oauth.time, "time", lambda: now[0])

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/login/device/code":
            assert request.content == b"client_id=test-client"
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        assert b"device_code=private-device" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 28800,
            },
        )

    original = github_oauth.begin
    monkeypatch.setattr(
        github_oauth,
        "begin",
        lambda client_id: original(client_id, httpx.Client(transport=httpx.MockTransport(handle))),
    )
    original_poll = github_oauth.poll
    monkeypatch.setattr(
        github_oauth,
        "poll",
        lambda flow, client_id, client=None: original_poll(
            flow, client_id, client or httpx.Client(transport=httpx.MockTransport(handle))
        ),
    )
    return calls, now


def test_device_login_reuses_grant_across_workspaces_but_not_owners(
    tmp_path: Path,
    github_login: Any,
) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "auth.json")
    auth = service.auth
    login = auth.account_login(service.principal, service.store.clio_id, "github")
    started = auth.start(login)
    assert started["user_code"] == "ABCD-EFGH"
    assert "private-device" not in json.dumps(started)
    wrong = login.model_copy(deep=True)
    wrong.principal = "another-user"
    with pytest.raises(PermissionError, match="another source, user"):
        auth.complete(wrong, started["flow_id"], "")
    assert auth.complete(login, started["flow_id"], "")
    assert auth.account_connected(service.principal, service.store.clio_id, "github")
    assert not auth.account_connected("another-user", service.store.clio_id, "github")
    for workspace in ("one", "two"):
        source = service.create(
            workspace,
            CreateSource(
                provider="github", root="https://github.com/test/private", label="Private data"
            ),
        )
        assert auth.token(source) == "private-access"
        assert "private-access" not in source.model_dump_json()
    restarted = StorageAuth(tmp_path / "auth.json")
    assert restarted.account_connected(service.principal, service.store.clio_id, "github")
    restarted.sign_out(service.principal, service.store.clio_id, "github")
    assert not restarted.connected(source)


def test_poll_obeys_pending_and_slow_down_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(github_oauth.time, "time", lambda: now[0])
    replies = iter(
        [
            {"error": "authorization_pending"},
            {"error": "slow_down", "interval": 12},
            {"access_token": "test-token"},
        ]
    )
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=next(replies))

    flow = github_oauth.DeviceAuthorization(
        "device", "CODE", "https://github.com/login/device", 1900, 5
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert github_oauth.poll(flow, "client", client) is None
        assert github_oauth.poll(flow, "client", client) is None
        assert len(calls) == 1
        now[0] += 5
        assert github_oauth.poll(flow, "client", client) is None
        assert flow.interval == 12
        now[0] += 11
        assert github_oauth.poll(flow, "client", client) is None
        assert len(calls) == 2
        now[0] += 1
        assert github_oauth.poll(flow, "client", client)["non_expiring"] is True


@pytest.mark.parametrize(
    "error,message",
    [
        ("access_denied", "cancelled"),
        ("expired_token", "expired"),
        ("incorrect_device_code", "rejected"),
    ],
)
def test_device_rejection_does_not_save_tokens(
    tmp_path: Path, github_login: Any, error: str, message: str
) -> None:
    auth = StorageAuth(tmp_path / "auth.json")
    login = auth.account_login("user", "host", "github")
    started = auth.start(login)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": error}))
    ) as client:
        with pytest.raises(ValueError, match=message):
            auth.complete(login, started["flow_id"], "", client=client)
    assert not auth.account_connected("user", "host", "github")


def test_private_fsspec_reads_and_cached_files_require_the_saved_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github_login: Any,
) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "auth.json")
    calls: list[Any] = []
    data = b"private bytes"
    sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(kwargs.get("auth"))
        response = requests.Response()
        response.url = url
        response.status_code = (
            200 if kwargs.get("auth") == ("x-access-token", "private-access") else 404
        )
        if url.endswith("/repos/test/private"):
            body = {"default_branch": "main"}
        elif "/git/trees/" in url:
            body = {
                "tree": [
                    {
                        "path": "secret.txt",
                        "type": "blob",
                        "mode": "100644",
                        "size": len(data),
                        "sha": sha,
                    }
                ]
            }
        else:
            body = {"content": base64.b64encode(data).decode()}
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr("fsspec.implementations.github.requests.get", get)
    source = service.create(
        "one",
        CreateSource(
            provider="github", root="https://github.com/test/private", label="Private data"
        ),
    )
    with pytest.raises(PermissionError, match="Sign in to GitHub"):
        link_folder(service, source)
    login = service.auth.account_login(service.principal, service.store.clio_id, "github")
    started = service.auth.start(login)
    service.auth.complete(login, started["flow_id"], "")
    linked = link_folder(service, source)
    assert linked_file(service, linked, "secret.txt").read_bytes() == data
    assert calls[-1] == ("x-access-token", "private-access")
    service.auth.sign_out(service.principal, service.store.clio_id, "github")
    service.github.clear()
    with pytest.raises(PermissionError, match="Sign in to GitHub"):
        linked_file(service, linked, "secret.txt")


def test_device_refresh_does_not_send_a_client_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github_login: Any
) -> None:
    _, now = github_login
    auth = StorageAuth(tmp_path / "auth.json")
    login = auth.account_login("user", "host", "github")
    auth.complete(login, auth.start(login)["flow_id"], "")
    now[0] += 28800
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=refresh_token" in request.content
        assert b"client_secret" not in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-token",
                "refresh_token": "rotated-refresh",
                "expires_in": 28800,
            },
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **_: original_client(transport=httpx.MockTransport(handle))
    )
    assert auth.token(login) == "rotated-token"


def test_provider_device_login_http_and_revoked_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github_login: Any
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    app = FastAPI()
    app.state.workspaces = {}
    register_connected_storage_routes(app)
    service = app.state.connected_storage
    prefix = "/v1/storage/accounts/github"
    with TestClient(app) as client:
        started = client.post(prefix + "/auth/start", json={})
        assert started.status_code == 200
        assert "private-device" not in started.text
        complete = client.post(
            prefix + "/auth/complete", json={"flow_id": started.json()["flow_id"]}
        )
        assert complete.json() == {"authenticated": True}
        providers = client.get("/v1/storage/providers")
        github = next(row for row in providers.json()["providers"] if row["id"] == "github")
        assert github["authenticated"] and github["authentication"] == "browser"
        assert "working_copy" in github["capabilities"]["supported_modes"]
        assert "private-access" not in providers.text
        login = service.auth.account_login(service.principal, service.store.clio_id, "github")
        service.auth.reject_token(login, "older-token")
        assert service.auth.connected(login)
        service.auth.reject_token(login, "private-access")
        assert not service.auth.connected(login)
        assert client.delete(prefix).json() == {"signed_out": True}
