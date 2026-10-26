"""Consent ownership/callback boundaries around the real SDK transport factory."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from clio_agent.gact import mcp_auth
from clio_agent.gact.routes.mcp_auth import _redirect_uri
from clio_agent.tools.mcp_config import MCPAuthConfig, MCPTransportError
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage
from clio_agent.tools.mcp_runtime import _oauth_provider_from_config


def test_sign_in_errors_explain_recovery_without_disclosing_provider_payloads() -> None:
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError

    assert "identity" in mcp_auth._sign_in_error(OAuthFlowError("issuer mismatch: private-code"))
    assert "registered OAuth client" in mcp_auth._sign_in_error(OAuthRegistrationError("secret"))
    assert "private-code" not in mcp_auth._sign_in_error(RuntimeError("private-code"))


@pytest.mark.asyncio
async def test_fresh_sdk_client_restores_expiry_and_refreshes_at_the_authorization_server(
    tmp_path: Path,
) -> None:
    storage = DurableFileTokenStorage(
        "https://resource.example.test/mcp", path=tmp_path / "tokens.json"
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="test-access",
            refresh_token="test-refresh",
            token_type="Bearer",
            expires_in=60,
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="fixture-client",
            issuer="https://auth.example.test/",
            redirect_uris=["http://localhost/return"],
            token_endpoint_auth_method="none",
        )
    )
    storage.save_authorization_context(
        {
            "issuer": "https://auth.example.test/",
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        },
        "https://auth.example.test/",
    )
    fresh = DurableFileTokenStorage(
        "https://resource.example.test/mcp", path=tmp_path / "tokens.json"
    )
    provider = _oauth_provider_from_config(
        "https://resource.example.test/mcp",
        MCPAuthConfig(
            client_metadata={"redirect_uris": ["http://localhost/return"]},
            storage=fresh,
            require_existing_tokens=True,
        ),
    )
    assert provider is not None
    await provider._initialize()
    assert provider.context.token_expiry_time == storage.token_expiry()
    request = await provider._refresh_token()
    assert str(request.url) == "https://auth.example.test/token"


@pytest.mark.asyncio
async def test_sign_out_blocks_an_existing_sdk_provider(tmp_path: Path) -> None:
    import httpx2

    storage = DurableFileTokenStorage("https://example.test/mcp", path=tmp_path / "tokens.json")
    await storage.set_tokens(OAuthToken(access_token="fixture-only", token_type="Bearer"))
    provider = _oauth_provider_from_config(
        "https://example.test/mcp",
        MCPAuthConfig(
            client_metadata={"redirect_uris": ["http://localhost/oauth/mcp"]},
            storage=storage,
            require_existing_tokens=True,
        ),
    )
    assert provider is not None
    storage.clear()
    await storage.set_tokens(OAuthToken(access_token="late-refresh", token_type="Bearer"))
    assert not storage.has_tokens()
    fresh = DurableFileTokenStorage("https://example.test/mcp", path=tmp_path / "tokens.json")
    await fresh.set_tokens(OAuthToken(access_token="new-consent", token_type="Bearer"))
    assert fresh.has_tokens()
    assert not storage.has_tokens()
    with pytest.raises(MCPTransportError, match="sign-in required"):
        await anext(provider.async_auth_flow(httpx2.Request("POST", "https://example.test/mcp")))


@pytest.mark.asyncio
async def test_saved_connections_at_same_url_keep_accounts_and_revocation_separate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tokens.json"
    first = DurableFileTokenStorage("https://same.test/mcp", path=path, connection_id="first")
    second = DurableFileTokenStorage("https://same.test/mcp", path=path, connection_id="second")
    legacy = DurableFileTokenStorage("https://same.test/mcp", path=path)
    await first.set_tokens(OAuthToken(access_token="first-account", token_type="Bearer"))
    await second.set_tokens(OAuthToken(access_token="second-account", token_type="Bearer"))
    await legacy.set_tokens(OAuthToken(access_token="legacy-account", token_type="Bearer"))
    first.clear()
    await first.set_tokens(OAuthToken(access_token="late-refresh", token_type="Bearer"))
    assert not first.has_tokens()
    assert (await second.get_tokens()).access_token == "second-account"
    assert (await legacy.get_tokens()).access_token == "legacy-account"
    fresh = DurableFileTokenStorage("https://same.test/mcp", path=path, connection_id="first")
    assert await fresh.get_tokens() is None
    await fresh.set_tokens(OAuthToken(access_token="new-first-account", token_type="Bearer"))
    assert fresh.has_tokens()
    assert not first.has_tokens()
    assert (await second.get_tokens()).access_token == "second-account"


@pytest.fixture
def handshake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    spec = {"transport": "http", "url": "https://mcp.example.test/mcp"}
    observed: dict[str, Any] = {"spec": spec, "closed": 0, "callback": None}
    monkeypatch.setattr(mcp_auth, "get_user_mcp_server", lambda _name: observed["spec"])
    monkeypatch.setattr(
        mcp_auth,
        "DurableFileTokenStorage",
        lambda url, **kwargs: DurableFileTokenStorage(url, path=tmp_path / "tokens.json", **kwargs),
    )
    monkeypatch.setattr(mcp_auth, "spec_from_declaration", lambda _name, value: value)
    monkeypatch.setattr(mcp_auth, "transport_for", lambda value: value)

    class Client:
        def __init__(self, target: dict[str, Any], **_kwargs: Any) -> None:
            self.auth = target["auth"]

        async def __aenter__(self) -> Client:
            try:
                await self.auth.redirect_handler(
                    "https://accounts.example.test/authorize?state=exact-state"
                )
                callback = await self.auth.callback_handler()
                observed["callback"] = callback
                await self.auth.storage.set_tokens(
                    OAuthToken(access_token="test-only", token_type="Bearer")
                )
                return self
            except BaseException:
                observed["closed"] += 1
                raise

        async def list_tools(self) -> list[Any]:
            return [SimpleNamespace(name="read_state")]

        async def __aexit__(self, *_args: Any) -> None:
            observed["closed"] += 1

    monkeypatch.setattr(mcp_auth, "make_mcp_client", Client)
    yield observed


@pytest.mark.asyncio
async def test_consent_is_exact_one_use_and_only_connected_after_handshake(
    handshake: dict[str, Any],
) -> None:
    refreshed: list[bool] = []

    async def refresh() -> None:
        refreshed.append(True)

    owner = mcp_auth.McpSignIns(refresh)
    try:
        started = await owner.start("home", "http://localhost:5173/oauth/mcp")
        assert started["state"] == "authorizing" and not started["tools"]
        flow_id = started["flow_id"]
        for name, callback in [
            ("other", "http://localhost:5173/oauth/mcp?state=exact-state&code=secret"),
            ("home", "http://localhost:5173/oauth/mcp?state=wrong&code=secret"),
            ("home", "http://evil.test/oauth/mcp?state=exact-state&code=secret"),
        ]:
            with pytest.raises(ValueError):
                await owner.complete(name, flow_id, callback)
        assert handshake["callback"] is None
        callback = "http://localhost:5173/oauth/mcp?state=exact-state&code=secret&iss=https%3A%2F%2Faccounts.example.test"
        result = await owner.complete("home", flow_id, callback)
        assert result["state"] == "connecting"
        await owner.get(flow_id).task
        finished = owner.get(flow_id).public()
        assert finished["state"] == "connected" and finished["tools"] == ["read_state"]
        assert refreshed == [True] and handshake["closed"] == 1
        assert handshake["callback"].iss == "https://accounts.example.test"
        assert "secret" not in str(finished) and "test-only" not in str(finished)
        with pytest.raises(ValueError):
            await owner.complete("home", flow_id, callback)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_changed_connection_cannot_consume_old_consent_and_cancel_retires_client(
    handshake: dict[str, Any],
) -> None:
    owner = mcp_auth.McpSignIns()
    started = await owner.start("home", "http://localhost:5173/oauth/mcp")
    handshake["spec"] = {**handshake["spec"], "url": "https://different.test/mcp"}
    with pytest.raises(ValueError, match="saved connection"):
        await owner.complete(
            "home", started["flow_id"], "http://localhost:5173/oauth/mcp?state=exact-state&code=x"
        )
    await owner.close()
    assert handshake["closed"] == 1


@pytest.mark.asyncio
async def test_denial_and_expiry_never_report_connection(
    handshake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = mcp_auth.McpSignIns()
    first = await owner.start("home", "http://localhost:5173/oauth/mcp")
    denied = await owner.complete(
        "home",
        first["flow_id"],
        "http://localhost:5173/oauth/mcp?state=exact-state&error=access_denied",
    )
    assert denied["state"] == "denied" and not denied["tools"]
    monkeypatch.setattr(mcp_auth, "SIGN_IN_SECONDS", 0.02)
    second = await owner.start("home", "http://localhost:5173/oauth/mcp")
    await asyncio.sleep(0.04)
    assert owner.get(second["flow_id"]).state == "expired"
    await owner.close()


@pytest.mark.parametrize(
    "uri,origin",
    [
        ("https://clio.test/oauth/mcp", "https://clio.test"),
        ("http://127.0.0.1:12345/clio-storage-return", ""),
    ],
)
def test_return_address_uses_initiator_or_local_receiver(uri: str, origin: str) -> None:
    assert _redirect_uri(uri, origin) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "https://wrong.test/oauth/mcp",
        "https://user:pass@clio.test/oauth/mcp",
        "https://clio.test/oauth/mcp?other=1",
        "file:///oauth/mcp",
    ],
)
def test_unrelated_callback_destinations_are_rejected(uri: str) -> None:
    with pytest.raises(ValueError):
        _redirect_uri(uri, "https://clio.test")
