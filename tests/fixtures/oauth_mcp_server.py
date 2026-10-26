"""Synthetic, read-only OAuth MCP for real browser acceptance; never a production provider.

Run with ``uv run python -B tests/fixtures/oauth_mcp_server.py --port 18921``.
The MCP SDK owns registration, redirect validation and PKCE verification. This
fixture's consent page explicitly identifies its synthetic account and tools.
"""

from __future__ import annotations

import argparse
import html
import secrets
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


class ShowcaseOAuth(OAuthProvider):
    """Ephemeral test account; all credentials disappear when this process retires."""

    def __init__(self, base: str) -> None:
        super().__init__(
            base_url=base,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["read"], default_scopes=["read"]
            ),
        )
        self.base = base
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.access["qualification-header-token"] = AccessToken(
            token="qualification-header-token",
            client_id="manual-fixture",
            scopes=["read"],
            expires_at=int(time.time()) + 3600,
        )
        self.refresh: dict[str, RefreshToken] = {}
        self.counts = {
            "registered": 0,
            "approved": 0,
            "denied": 0,
            "exchanged": 0,
            "refreshed": 0,
            "tool_calls": 0,
        }

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return a registered test client."""
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Retain DCR metadata in this fixture process only."""
        self.clients[client_info.client_id] = client_info
        self.counts["registered"] += 1

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Show explicit consent rather than silently accepting the test account."""
        nonce = secrets.token_urlsafe(24)
        self.pending[nonce] = client, params
        return f"{self.base}/consent?id={nonce}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Let the SDK validate PKCE and the exact redirect before exchanging."""
        return self.codes.get(authorization_code)

    def issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        """Issue short-lived fixture credentials to exercise automatic refresh."""
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=int(time.time()) + 15
        )
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=15,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Consume the code once, after SDK verification."""
        self.codes.pop(authorization_code.code)
        self.counts["exchanged"] += 1
        return self.issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Return a fixture refresh token."""
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate test refresh credentials."""
        self.refresh.pop(refresh_token.token)
        self.counts["refreshed"] += 1
        return self.issue(client.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Reject expired fixture credentials."""
        value = self.access.get(token)
        return value if value and value.expires_at and value.expires_at > time.time() else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Retire the exact fixture token."""
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)


def application(port: int, *, stdio: bool = False) -> Any:
    """Build the actual HTTP MCP and consent page for a contained acceptance run."""
    auth = ShowcaseOAuth(f"http://127.0.0.1:{port}")
    mcp = FastMCP("Synthetic OAuth qualification", auth=None if stdio else auth)

    @mcp.tool()
    def fixture_status() -> dict[str, Any]:
        """Read the synthetic test account; performs no external actions."""
        auth.counts["tool_calls"] += 1
        return {"account": "Synthetic qualification account", "read_only": True}

    @mcp.custom_route("/consent", methods=["GET"])
    async def consent(request: Request) -> Response:
        nonce = request.query_params.get("id", "")
        found = auth.pending.get(nonce)
        if not found:
            return HTMLResponse("This fixture sign-in has expired.", status_code=410)
        client, params = found
        decision = request.query_params.get("decision")
        if decision in {"allow", "deny"}:
            auth.pending.pop(nonce)
            if decision == "deny":
                auth.counts["denied"] += 1
                return RedirectResponse(
                    construct_redirect_uri(
                        str(params.redirect_uri), error="access_denied", state=params.state
                    )
                )
            auth.counts["approved"] += 1
            code = secrets.token_urlsafe(32)
            auth.codes[code] = AuthorizationCode(
                code=code,
                client_id=client.client_id,
                expires_at=time.time() + 60,
                scopes=params.scopes or [],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
            )
            return RedirectResponse(
                construct_redirect_uri(
                    str(params.redirect_uri),
                    code=code,
                    state=params.state,
                    iss=str(auth.issuer_url),
                )
            )
        return HTMLResponse(
            f"""<!doctype html><html><head><title>Synthetic MCP sign-in</title></head><body style="font:18px system-ui;background:#f5fafb;color:#142b35;padding:8vh 24px"><main style="max-width:36rem;margin:auto;background:white;border:1px solid #cadce3;border-radius:16px;padding:32px"><p>CLIO · OAuth protocol fixture</p><h1>Connect the test account?</h1><p>This is a synthetic account for qualification. It exposes one read-only tool and cannot access Home Assistant or other services.</p><p><strong>{html.escape(client.client_name or "CLIO")}</strong> requests access to read the fixture status.</p><p><a style="display:inline-block;background:#007e8c;color:white;padding:12px 20px;border-radius:8px;text-decoration:none" href="?id={nonce}&amp;decision=allow">Allow test connection</a> <a href="?id={nonce}&amp;decision=deny">Deny</a></p></main></body></html>"""
        )

    @mcp.custom_route("/fixture-counters", methods=["GET"])
    async def counters(_request: Request) -> JSONResponse:
        return JSONResponse(auth.counts)

    return mcp if stdio else mcp.http_app(path="/mcp", stateless_http=True)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18921)
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if args.stdio:
        application(args.port, stdio=True).run(transport="stdio", show_banner=False)
    else:
        uvicorn.run(application(args.port), host="127.0.0.1", port=args.port, access_log=False)
