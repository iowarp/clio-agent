"""The MCP SDK OAuth provider over CLIO's durable token storage.

Imported lazily by :func:`clio_agent.tools.mcp_runtime._oauth_provider_from_config`
(at the client factory boundary), so the MCP SDK loads only when a server needs OAuth.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx2
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.auth.utils import credentials_match_issuer
from mcp.shared.auth import OAuthMetadata

from clio_agent.tools.mcp_config import MCPTransportError
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage


class DurableOAuthProvider(OAuthClientProvider):
    """Restore SDK discovery/expiry and recheck consent across client lifetimes."""

    def __init__(
        self, *, storage: DurableFileTokenStorage, sign_in_required: bool, **kwargs: Any
    ) -> None:
        """``sign_in_required``: refuse to start a browser flow when no tokens are saved."""
        super().__init__(storage=storage, **kwargs)
        self._durable = storage
        self._sign_in_required = sign_in_required
        self._client_metadata_url = kwargs.get("client_metadata_url")
        self._recorded_token = ""

    async def _initialize(self) -> None:
        await super()._initialize()
        saved = self._durable.authorization_context()
        if saved:
            metadata = OAuthMetadata.model_validate(saved.get("metadata"))
            issuer = str(saved.get("issuer") or metadata.issuer)
            if self.context.client_info and not credentials_match_issuer(
                self.context.client_info, issuer, self._client_metadata_url
            ):
                raise MCPTransportError(
                    "Saved OAuth client does not match the authorization server. Sign in again in CLIO"
                )
            self.context.oauth_metadata = metadata
            self.context.auth_server_url = issuer
            self.context.token_expiry_time = self._durable.token_expiry()
            if self.context.current_tokens:
                self._recorded_token = self.context.current_tokens.access_token

    def _record_authorization(self) -> None:
        tokens, metadata = self.context.current_tokens, self.context.oauth_metadata
        if (
            tokens
            and metadata
            and tokens.access_token != self._recorded_token
            and self._durable.has_tokens()
        ):
            self._durable.save_authorization_context(
                metadata.model_dump(mode="json", exclude_none=True),
                self.context.auth_server_url,
            )
            self._recorded_token = tokens.access_token

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if self._sign_in_required and not self._durable.has_tokens():
            raise MCPTransportError("MCP sign-in required; connect this service in CLIO")
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                self._record_authorization()
                response = yield outgoing
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()
