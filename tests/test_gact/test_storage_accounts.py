"""Reusable accounts retain strict user, host, provider and source-access boundaries."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage.accounts import source_binding
from clio_agent.gact.storage.auth import OAuthApplication, StorageAuth
from clio_agent.gact.storage.models import SourceRecord


def record(
    provider: str = "google_drive", identifier: str = "s1", workspace: str = "w1"
) -> SourceRecord:
    """Create an independently approved source in the same user's CLIO."""
    return SourceRecord.model_validate(
        {
            "principal": "alice",
            "source": {
                "id": identifier,
                "workspace_id": workspace,
                "label": identifier,
                "provider": provider,
                "root": identifier,
                "owner": {"clio_id": "clio1", "host_id": "local"},
                "capabilities": {},
            },
        }
    )


def sign_in(auth: StorageAuth, source: SourceRecord, token: str = "test-access") -> None:
    """Exercise PKCE with only the external OAuth HTTP response replaced."""
    app = OAuthApplication(
        source.source.provider,
        "test-client",
        "https://test/callback",
        "https://test/authorize",
        "https://test/token",
    )
    flow = auth.start(source, app)
    state = parse_qs(urlparse(flow["authorization_url"]).query)["state"][0]
    data = {"access_token": token, "refresh_token": "test-refresh", "expires_in": 3600}
    if source.source.provider == "globus":
        data["resource_server"] = "transfer.api.globus.org"
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as client:
        auth.complete(
            source,
            flow["flow_id"],
            "https://test/callback?" + urlencode({"state": state, "code": "test-code"}),
            client=client,
        )


@pytest.mark.parametrize("provider", ["google_drive", "globus"])
def test_saved_login_reused_across_workspaces_after_restart(tmp_path: Path, provider: str) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    first = record(provider)
    sign_in(auth, first)
    second = record(provider, "s2", "w2")
    restarted = StorageAuth(tmp_path / "credentials.json")
    assert restarted.connected(second)
    assert restarted.token(second) == "test-access"
    restarted.disconnect(first.source.id)
    assert not restarted.connected(first)
    assert restarted.connected(second)
    assert restarted.account_connected("alice", "clio1", provider)
    restarted.reconnect(first.source.id)
    assert restarted.connected(first)
    assert "test-access" not in second.model_dump_json()


@pytest.mark.parametrize("boundary", ["principal", "clio_id", "host_id", "provider"])
def test_accounts_do_not_cross_ownership_boundaries(tmp_path: Path, boundary: str) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    sign_in(auth, record())
    other = record(identifier="s2")
    if boundary == "principal":
        other.principal = "bob"
    elif boundary == "provider":
        other.source = other.source.model_copy(update={"provider": "globus"})
    else:
        other.source = other.source.model_copy(
            update={"owner": other.source.owner.model_copy(update={boundary: "other"})}
        )
    assert not auth.connected(other)
    with pytest.raises(PermissionError):
        auth.token(other)


def test_readonly_drive_login_needs_more_consent_for_writes(tmp_path: Path) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    sign_in(auth, record())
    writer = record(identifier="writer")
    writer.source = writer.source.model_copy(update={"mode": "working_copy"})
    assert not auth.connected(writer)
    sign_in(auth, writer, "write-token")
    assert auth.connected(writer)
    assert auth.token(record(identifier="reader")) == "write-token"


def test_legacy_migration_retains_consent_and_account_after_discard(tmp_path: Path) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    original = record("globus")
    auth._vault.write_entries(
        {
            original.source.id: {
                **source_binding(original),
                "tokens": {
                    "access_token": "legacy-token",
                    "refresh_token": "legacy-refresh",
                    "expires_at": time.time() - 1,
                },
                "consent_required": True,
                "consent_scopes": ["collection-consent"],
            }
        }
    )
    assert auth.account_connected("alice", "clio1", "globus")
    assert not auth.connected(original)
    other = record("globus", "other", "w2")
    assert auth.connected(other)
    entries = auth._vault.read_entries()
    assert "tokens" not in entries[original.source.id]
    assert entries[original.source.id]["consent_scopes"] == ["collection-consent"]
    assert len([row for row in entries.values() if row.get("kind") == "account"]) == 1
    auth.disconnect(original.source.id)
    restarted = StorageAuth(tmp_path / "credentials.json")
    assert restarted.connected(other)
    assert not restarted.connected(original)


def test_new_login_does_not_switch_other_sources_to_a_different_account(tmp_path: Path) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    first = record()
    sign_in(auth, first, "first-account")
    second = record(identifier="s2")
    assert auth.connected(second)
    sign_in(auth, first, "second-account")
    assert auth.token(second) == "first-account"
    assert auth.token(first) == "second-account"
    assert auth.token(record(identifier="s3")) == "second-account"


def test_provider_signout_clears_all_its_logins_and_pending_flows(tmp_path: Path) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    first = record("globus")
    sign_in(auth, first)
    second = record("globus", "s2", "w2")
    assert auth.connected(second)
    drive = record(identifier="drive")
    sign_in(auth, drive)
    pending = auth.start(first)
    auth.sign_out("alice", "clio1", "globus")
    assert not auth.connected(first)
    assert not auth.connected(second)
    assert not auth.account_connected("alice", "clio1", "globus")
    assert auth.connected(drive)
    with pytest.raises(ValueError, match="expired"):
        auth.complete(first, pending["flow_id"], "unused-code")


@pytest.mark.parametrize("action", ["signout", "remove"])
def test_discard_during_exchange_cannot_restore_account(tmp_path: Path, action: str) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    source = record("globus")
    flow = auth.start(source)

    def response(_: httpx.Request) -> httpx.Response:
        if action == "signout":
            auth.sign_out("alice", "clio1", "globus")
        else:
            auth.disconnect(source.source.id)
        return httpx.Response(
            200, json={"resource_server": "transfer.api.globus.org", "access_token": "too-late"}
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(PermissionError, match="cancelled"):
            auth.complete(source, flow["flow_id"], "test-code", client=client)
    assert not auth.account_connected("alice", "clio1", "globus")


def test_expired_or_rejected_account_is_not_reported_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    source = record()
    sign_in(auth, source)
    entries = auth._vault.read_entries()
    account = next(row for row in entries.values() if row.get("kind") == "account")
    account["tokens"] = {"access_token": "expired", "expires_at": 1}
    auth._vault.write_entries(entries)
    assert not auth.account_connected("alice", "clio1", "google_drive")
    account["tokens"]["refresh_token"] = "revoked"
    auth._vault.write_entries(entries)
    assert auth.account_connected("alice", "clio1", "google_drive")

    def reject(*args: object, **kwargs: object) -> dict[str, object]:
        raise ValueError("Storage authorization was rejected; start sign-in again")

    monkeypatch.setattr(auth, "_exchange", reject)
    with pytest.raises(ValueError, match="rejected"):
        auth.token(source)
    assert not auth.account_connected("alice", "clio1", "google_drive")


def test_provider_status_and_new_workspace_reuse_real_http_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    app = FastAPI()
    app.state.workspaces = {}
    for wid in ("w1", "w2"):
        root = tmp_path / wid
        root.mkdir()
        app.state.workspaces[wid] = SimpleNamespace(root_path=str(root))
    register_connected_storage_routes(app)
    service = app.state.connected_storage
    body = {
        "provider": "globus",
        "label": "Sample",
        "root": "/",
        "configuration": {"collection_id": "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"},
    }
    with TestClient(app) as client:
        first = client.post("/v1/workspaces/w1/sources", json=body).json()
        original = service.get("w1", first["id"])
        sign_in(service.auth, original)
        providers = client.get("/v1/storage/providers")
        assert next(row for row in providers.json()["providers"] if row["id"] == "globus")[
            "authenticated"
        ]
        assert "test-access" not in providers.text
        second = client.post("/v1/workspaces/w2/sources", json=body).json()
        assert second["authenticated"]
        assert client.delete(f"/v1/workspaces/w1/sources/{first['id']}").status_code == 200
        assert client.get("/v1/workspaces/w1/sources").json()["sources"] == []
        prefix = f"/v1/workspaces/w2/sources/{second['id']}"
        assert client.post(prefix + "/disconnect").json()["authenticated"] is False
        assert client.post(prefix + "/reconnect").json()["authenticated"] is True
        assert client.delete("/v1/storage/accounts/globus").json() == {"signed_out": True}
        assert (
            client.get("/v1/workspaces/w2/sources").json()["sources"][0]["authenticated"] is False
        )
        assert client.delete("/v1/storage/accounts/local").status_code == 404


def test_provider_only_signin_creates_no_workspace_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    app = FastAPI()
    app.state.workspaces = {}
    register_connected_storage_routes(app)
    service = app.state.connected_storage

    def exchange(
        app: OAuthApplication, payload: dict[str, str], *args: object, **kwargs: object
    ) -> dict[str, object]:
        assert app.provider == "globus"
        assert payload["code"] == "user-entered-code"
        assert "code_verifier" in payload
        return {
            "access_token": "account-token",
            "refresh_token": "account-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(service.auth, "_exchange", exchange)
    with TestClient(app) as client:
        flow = client.post("/v1/storage/accounts/globus/auth/start", json={})
        assert flow.status_code == 200
        complete = client.post(
            "/v1/storage/accounts/globus/auth/complete",
            json={
                "flow_id": flow.json()["flow_id"],
                "callback_url": "user-entered-code",
            },
        )
        assert complete.json() == {"authenticated": True}
        assert service.store.list("source", SourceRecord) == []
        assert service.auth.account_connected(service.principal, service.store.clio_id, "globus")
        assert "account-token" not in client.get("/v1/storage/providers").text
