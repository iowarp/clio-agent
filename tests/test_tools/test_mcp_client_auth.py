"""Remote MCP client authentication coverage for headers and OAuth (#1118)."""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthToken

from clio_agent.tools.mcp_config import (
    MCPAuthConfig,
    MCPServerSpec,
    MCPTransportError,
    redact_mcp_spec,
    transport_for,
    transport_from_spec,
)
from clio_agent.tools.mcp_handlers import MCPClientCapabilities
from clio_agent.tools.mcp_runtime import make_mcp_client


class _FakeClient:
    """Capture the factory-normalized transport without opening a connection."""

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self.target = target
        self.kwargs = kwargs


class _MemoryStorage:
    """In-memory implementation of the MCP SDK TokenStorage protocol for tests."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


def _oauth_runtime_spec(
    storage: _MemoryStorage,
    redirect_handler: Any,
    callback_handler: Any,
) -> dict[str, Any]:
    """Return the runtime-dict OAuth surface consumed by the client factory."""
    return {
        "transport": "http",
        "url": "https://mcp.example/mcp",
        "headers": {"X-Tenant": "science"},
        "auth": {
            "type": "oauth",
            "client_metadata": {
                "client_name": "clio-agent",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "tools",
            },
            "storage": storage,
            "redirect_handler": redirect_handler,
            "callback_handler": callback_handler,
        },
    }


@pytest.mark.asyncio
async def test_oauth_fake_authorization_server_authenticates_and_refreshes() -> None:
    """SDK OAuth code grant attaches a token, then refreshes it in memory."""
    storage = _MemoryStorage()
    callback_state = ""
    token_grants: list[str] = []
    mcp_authorizations: list[str] = []

    async def redirect_handler(authorization_url: str) -> None:
        nonlocal callback_state
        callback_state = parse_qs(urlparse(authorization_url).query)["state"][0]

    async def callback_handler() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code="authorization-code", state=callback_state)

    async def fake_authorization_server(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/mcp":
            authorization = request.headers.get("Authorization", "")
            mcp_authorizations.append(authorization)
            if not authorization:
                return httpx2.Response(401, headers={"WWW-Authenticate": "Bearer"})
            return httpx2.Response(200, json={"authenticated": True})
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(
                200,
                json={
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://mcp.example"],
                    "scopes_supported": ["tools"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://mcp.example",
                    "authorization_endpoint": "https://mcp.example/authorize",
                    "token_endpoint": "https://mcp.example/token",
                    "registration_endpoint": "https://mcp.example/register",
                    "scopes_supported": ["tools"],
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register":
            return httpx2.Response(
                201,
                json={
                    "client_id": "registered-client",
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
        if path == "/token":
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            token_grants.append(grant)
            if grant == "authorization_code":
                assert form["code"] == ["authorization-code"]
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "access-one",
                        "token_type": "Bearer",
                        "refresh_token": "refresh-one",
                        "expires_in": 3600,
                        "scope": "tools",
                    },
                )
            assert form["refresh_token"] == ["refresh-one"]
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-two",
                    "token_type": "Bearer",
                    "refresh_token": "refresh-two",
                    "expires_in": 3600,
                    "scope": "tools",
                },
            )
        raise AssertionError(f"unexpected fake OAuth request: {request.method} {request.url}")

    client = make_mcp_client(
        _oauth_runtime_spec(storage, redirect_handler, callback_handler),
        client_cls=_FakeClient,
    )
    provider = client.target.auth
    assert isinstance(provider, OAuthClientProvider)
    assert client.target.headers == {"X-Tenant": "science"}

    transport = httpx2.MockTransport(fake_authorization_server)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as http_client:
        response = await http_client.get("https://mcp.example/mcp")
        assert response.json() == {"authenticated": True}
        provider.context.token_expiry_time = 1
        refreshed = await http_client.get("https://mcp.example/mcp")
        assert refreshed.json() == {"authenticated": True}

    assert token_grants == ["authorization_code", "refresh_token"]
    assert mcp_authorizations == ["", "Bearer access-one", "Bearer access-two"]
    assert storage.tokens is not None
    assert storage.tokens.access_token == "access-two"
    assert storage.tokens.refresh_token == "refresh-two"


def test_typed_mcp_server_auth_block_reaches_http_transport() -> None:
    """The typed MCPServerSpec surface composes static headers and SDK OAuth."""
    storage = _MemoryStorage()
    auth = MCPAuthConfig(
        client_metadata={
            "client_name": "clio-agent",
            "redirect_uris": ["http://127.0.0.1/callback"],
        },
        storage=storage,
    )
    spec = MCPServerSpec(
        name="remote",
        transport="http",
        url="https://mcp.example/mcp",
        headers={"Authorization": "Bearer static-secret"},
        auth=auth,
    )

    transport = transport_for(spec)

    assert transport.headers == {"Authorization": "Bearer static-secret"}
    assert isinstance(transport.auth, OAuthClientProvider)
    assert "static-secret" not in repr(spec)
    assert "client_name" not in repr(auth)


def test_runtime_http_unknown_field_fails_typed_instead_of_vanishing() -> None:
    """Every runtime HTTP field is either consumed deliberately or rejected."""
    with pytest.raises(MCPTransportError, match=r"unsupported MCP HTTP field\(s\): verify"):
        transport_from_spec(
            {
                "transport": "http",
                "url": "https://mcp.example/mcp",
                "verify": False,
            }
        )


def test_credentials_are_absent_from_failure_trace_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid OAuth metadata fails without exposing headers or OAuth values."""
    header_secret = "header-secret-81d2"
    oauth_secret = "oauth-secret-46a9"
    spec = {
        "transport": "http",
        "url": "https://mcp.example/mcp",
        "headers": {"Authorization": f"Bearer {header_secret}"},
        "auth": {
            "type": "oauth",
            "client_metadata": {"client_name": oauth_secret},
        },
    }

    with caplog.at_level("DEBUG"), pytest.raises(MCPTransportError) as caught:
        transport_from_spec(spec)

    failure_text = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    observed = f"{failure_text}\n{caplog.text}\n{redact_mcp_spec(spec)!r}"
    assert header_secret not in observed
    assert oauth_secret not in observed
    assert f"Bearer {header_secret}" not in observed
    assert redact_mcp_spec(spec)["headers"] == {"Authorization": "<redacted>"}
    assert redact_mcp_spec(spec)["auth"] == "<redacted>"
    # env values are the common stdio credential carrier (GITHUB_TOKEN, API
    # keys): the canonical helper must redact them too — names kept, values gone.
    env_spec = {"transport": "stdio", "command": "x", "env": {"GITHUB_TOKEN": "ghp_secret"}}
    assert redact_mcp_spec(env_spec)["env"] == {"GITHUB_TOKEN": "<redacted>"}


def test_redaction_masks_credentials_env_expansion_wrote_into_argv_and_url() -> None:
    """``${VAR}`` expands INTO argv/command/url long before anything redacts them.

    ``headers``/``auth``/``env`` are masked by name, but a declaration like
    ``ndp-server --token ${NDP_TOKEN}`` reaches ``MCPServerSpec.args`` already
    holding the token, and ``redact_mcp_spec`` passed argv/url straight through --
    so the credential shipped on ``GET /v1/mcp/servers`` and into every typed
    error detail that carries a spec. The redacted view must serve the
    DECLARATION (names kept, values gone), exactly like ``env``.
    """

    from dataclasses import asdict

    from clio_agent.tools.mcp_config import spec_from_declaration

    token = "tok_super_secret_9f31"
    env = {"NDP_TOKEN": token, "NDP_HOST": "ndp.example", "NDP_BIN": "ndp-server"}

    stdio = spec_from_declaration(
        "ndp",
        "${NDP_BIN} --token ${NDP_TOKEN} --mode plain",
        source="workspace",
        env=env,
    )
    mapped = spec_from_declaration(
        "geo",
        {"command": "geo-server", "args": ["--key", "${NDP_TOKEN}", "--verbose"]},
        source="workspace",
        env=env,
    )
    http = spec_from_declaration(
        "web",
        {"type": "http", "url": "https://${NDP_HOST}/mcp?key=${NDP_TOKEN}"},
        source="workspace",
        env=env,
    )

    # The RUNTIME spec must still hold the real values — redaction is a view.
    assert stdio.command == "ndp-server"
    assert stdio.args == ("--token", token, "--mode", "plain")
    assert http.url == f"https://ndp.example/mcp?key={token}"

    for spec in (stdio, mapped, http):
        redacted = redact_mcp_spec(asdict(spec))
        assert token not in repr(redacted), f"{spec.name} leaked its expanded credential"
        # The pre-expansion declaration is what survives, so the row stays
        # readable and names the variable an operator has to fix.
        assert "declared" not in redacted, "the raw declaration must not ship as a second field"

    assert redact_mcp_spec(asdict(stdio))["args"] == ["--token", "${NDP_TOKEN}", "--mode", "plain"]
    assert redact_mcp_spec(asdict(mapped))["args"] == ["--key", "${NDP_TOKEN}", "--verbose"]
    assert redact_mcp_spec(asdict(http))["url"] == "https://${NDP_HOST}/mcp?key=${NDP_TOKEN}"
    # An expansion-free declaration is untouched: no false masking.
    plain = spec_from_declaration("fs", "fs-server --root /data", source="workspace", env=env)
    assert redact_mcp_spec(asdict(plain))["args"] == ["--root", "/data"]


def test_redaction_masks_argv_when_an_expansion_changes_its_shape() -> None:
    """A value carrying whitespace re-splits argv, so positions no longer align.

    Element-wise substitution would then hand back the wrong token; the only safe
    answer for a misaligned argv is to mask the whole vector rather than guess.
    """

    from dataclasses import asdict

    from clio_agent.tools.mcp_config import spec_from_declaration

    secret = "two words"
    spec = spec_from_declaration(
        "ndp", "ndp-server --token ${NDP_TOKEN}", source="workspace", env={"NDP_TOKEN": secret}
    )
    assert spec.args == ("--token", "two", "words"), "the expansion re-split argv"
    redacted = redact_mcp_spec(asdict(spec))
    assert secret not in repr(redacted)
    assert redacted["args"] == ["<redacted>", "<redacted>", "<redacted>"]


def test_factory_auth_wiring_has_one_source_owner() -> None:
    """No transport-level credential wiring exists outside the two owner modules."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    owners = {
        source_root / "tools" / "mcp_config.py",
        source_root / "tools" / "mcp_runtime.py",
    }
    forbidden = re.compile(
        r"(?:OAuthClientProvider|StreamableHttpTransport|SSETransport)\s*\([^)]*"
        r"(?:headers|auth)\s*=",
        re.DOTALL,
    )
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        if path in owners:
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            violations.append(str(path.relative_to(source_root)))
    assert violations == []


def test_proxy_capabilities_preserve_auth_and_transport_options() -> None:
    """OAuth transport auth composes with ProxyClient forwarding and capabilities."""
    from fastmcp.server.providers.proxy import ProxyClient, _ForwardingClientSession

    storage = _MemoryStorage()
    client = make_mcp_client(
        _oauth_runtime_spec(storage, None, None),
        capabilities=MCPClientCapabilities(elicitation_form=True),
        client_cls=ProxyClient,
    )

    assert isinstance(client.transport.auth, OAuthClientProvider)
    assert client._transport_options.forward_incoming_headers is True
    assert issubclass(client._transport_options.session_class, _ForwardingClientSession)
