"""PKCE sign-in isolation and sanitization at the provider HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities

from clio_agent.gact.storage.auth import OAuthApplication, StorageAuth
from clio_agent.gact.storage.auth import application as configured_application
from clio_agent.gact.storage.models import SourceRecord


def source() -> SourceRecord:
    """A test source whose ownership must survive the entire flow."""
    return SourceRecord(
        principal="alice",
        source=ConnectedSource(
            id="source1",
            owner=ResourceOwner(clio_id="clio1", host_id="local"),
            provider="google_drive",
            label="OPAL",
            root="folder1",
            capabilities=SourceCapabilities(),
        ),
    )


def application() -> OAuthApplication:
    """Use fixed local-test provider endpoints without contacting an OAuth service."""
    return OAuthApplication(
        "google_drive",
        "clio-client",
        "https://clio.test/callback",
        "https://provider.test/authorize",
        "https://provider.test/token",
    )


@pytest.mark.parametrize(
    "desktop_redirect",
    [None, "http://localhost:48173/clio-storage-return"],
)
def test_globus_sign_in_uses_shipped_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, desktop_redirect: str | None
) -> None:
    """Desktop and browser clients can begin sign-in without host OAuth setup."""
    for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"CLIO_STORAGE_GLOBUS_{suffix}", raising=False)
    record = source()
    record.source = record.source.model_copy(update={"provider": "globus"})
    auth = StorageAuth(tmp_path / "tokens.json")
    started = auth.start(record, desktop_redirect=desktop_redirect)
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["client_id"] == ["ca844cdc-ddb1-4332-bb66-69f9479d99b5"]
    assert query["redirect_uri"] == [desktop_redirect or "https://auth.globus.org/v2/web/auth-code"]
    assert query["code_challenge_method"] == ["S256"]
    assert not auth.connected(record)


def test_globus_registration_override_and_explicit_disable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Custom distributions can replace or explicitly disable the bundled identity."""
    monkeypatch.setenv("CLIO_STORAGE_GLOBUS_CLIENT_ID", "custom-client")
    assert configured_application("globus").client_id == "custom-client"
    monkeypatch.setenv("CLIO_STORAGE_GLOBUS_CLIENT_ID", "")
    record = source()
    record.source = record.source.model_copy(update={"provider": "globus"})
    with pytest.raises(ValueError, match="not been configured"):
        StorageAuth(tmp_path / "tokens.json").start(record)


def test_desktop_return_is_bound_to_loopback_and_pending_flow(tmp_path: Path) -> None:
    auth = StorageAuth(tmp_path / "private" / "auth.json")
    redirect = "http://127.0.0.1:49211/clio-storage-return"
    flow = auth.start(source(), application(), desktop_redirect=redirect)
    assert parse_qs(urlparse(flow["authorization_url"]).query)["redirect_uri"] == [redirect]
    assert auth._pending[flow["flow_id"]].app.redirect_uri == redirect
    for invalid in [
        "https://example.org/callback",
        "http://127.0.0.1:49211/other",
        "http://user@127.0.0.1:49211/clio-storage-return",
    ]:
        with pytest.raises(ValueError, match="Invalid Desktop"):
            auth.start(source(), application(), desktop_redirect=invalid)


def test_globus_requests_native_offline_scope_for_refresh(tmp_path: Path) -> None:
    """Globus uses offline_access; Google's access_type parameter is insufficient."""
    record = source()
    record.source = record.source.model_copy(update={"provider": "globus"})
    app = OAuthApplication(
        "globus",
        "clio-client",
        "https://auth.globus.org/v2/web/auth-code",
        "https://auth.globus.org/v2/oauth2/authorize",
        "https://auth.globus.org/v2/oauth2/token",
    )
    started = StorageAuth(tmp_path / "tokens.json").start(record, app)
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert set(query["scope"][0].split()) == {
        "urn:globus:auth:scope:transfer.api.globus.org:all",
        "offline_access",
    }
    assert "access_type" not in query
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.parametrize("changed", ["principal", "host", "clio", "source", "root", "mode"])
def test_sign_in_cannot_attach_to_a_different_owner_or_source(tmp_path: Path, changed: str) -> None:
    record = source()
    auth = StorageAuth(tmp_path / "private" / "tokens.json")
    started = auth.start(record, application())
    replacement = record.model_copy(deep=True)
    if changed == "principal":
        replacement.principal = "other"
    elif changed in {"host", "clio"}:
        replacement.source = record.source.model_copy(
            update={
                "owner": ResourceOwner(
                    clio_id="other" if changed == "clio" else "clio1",
                    host_id="other" if changed == "host" else "local",
                )
            }
        )
    else:
        replacement.source = record.source.model_copy(
            update={"id" if changed == "source" else changed: "other"}
        )
    with pytest.raises(PermissionError, match="another source"):
        auth.complete(replacement, started["flow_id"], "https://clio.test/callback?code=secret")
    assert not auth.connected(record)


def test_pkce_exchange_is_single_use_and_keeps_credentials_out_of_source(tmp_path: Path) -> None:
    record = source()
    auth = StorageAuth(tmp_path / "private" / "tokens.json")
    started = auth.start(record, application())
    query = parse_qs(urlparse(started["authorization_url"]).query)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
        )

    callback = "https://clio.test/callback?" + urlencode(
        {"state": query["state"][0], "code": "one-use-code"}
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        auth.complete(record, started["flow_id"], callback, client=client)
        with pytest.raises(ValueError, match="expired"):
            auth.complete(record, started["flow_id"], callback, client=client)
    assert len(requests) == 1
    body = parse_qs(requests[0].content.decode())
    assert len(body["code_verifier"][0]) >= 43
    assert query["code_challenge_method"] == ["S256"]
    assert auth.connected(record)
    assert auth.token(record) == "access-secret"
    assert "secret" not in record.model_dump_json()
    auth.disconnect(record.source.id)
    assert not auth.connected(record)


def test_wrong_state_does_not_exchange_or_leak_provider_error(tmp_path: Path) -> None:
    record = source()
    auth = StorageAuth(tmp_path / "tokens.json")
    started = auth.start(record, application())
    with pytest.raises(ValueError, match="invalid"):
        auth.complete(
            record, started["flow_id"], "https://clio.test/callback?state=wrong&code=secret"
        )
    query = parse_qs(urlparse(started["authorization_url"]).query)
    callback = "https://clio.test/callback?" + urlencode(
        {"state": query["state"][0], "code": "secret"}
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, text="do not log access-secret")
        )
    ) as client:
        with pytest.raises(ValueError, match="authorization was rejected") as error:
            auth.complete(record, started["flow_id"], callback, client=client)
    assert "access-secret" not in str(error.value)
