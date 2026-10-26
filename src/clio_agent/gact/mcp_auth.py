"""Bounded user-initiated MCP sign-in; protocol OAuth remains owned by the MCP SDK."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull

from clio_agent.gact.mcp_user_configuration import get_user_mcp_server
from clio_agent.tools.mcp_config import MCPAuthConfig, spec_from_declaration, transport_for
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage
from clio_agent.tools.mcp_runtime import make_mcp_client

SIGN_IN_SECONDS = 300
MAX_PENDING = 8
logger = logging.getLogger(__name__)


def _sign_in_error(error: BaseException) -> str:
    """Classify SDK failures without echoing URLs, authorization codes or tokens."""
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OAuthRegistrationError):
            return "The server could not register CLIO. Use a registered OAuth client in Advanced sign-in settings."
        if isinstance(current, OAuthTokenError):
            return "The authorization server rejected token exchange. Start again or check the registered client settings."
        if isinstance(current, OAuthFlowError) and "issuer" in str(current).lower():
            return "The sign-in server identity did not match its authorization metadata. Check the server's OAuth settings."
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )
    return "The MCP sign-in or connection check failed. Check the server settings and retry."


def managed_oauth(
    redirect_uri: str = "http://localhost/clio-mcp-return", *, connection_id: str | None = None
) -> dict[str, Any]:
    """Declare refreshable OAuth without allowing discovery to open an interactive flow."""
    return {
        "type": "oauth",
        "require_existing_tokens": True,
        **({"connection_id": connection_id} if connection_id else {}),
        "client_metadata": {
            "client_name": "CLIO",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    }


def configuration_digest(spec: dict[str, Any]) -> str:
    """Bind consent to the exact saved connection without exposing its contents."""
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


@dataclass
class McpSignIn:
    """One short-lived consent attempt, with secrets kept out of its public view."""

    id: str
    name: str
    revision: str
    redirect_uri: str
    expires_at: float
    state: str = "preparing"
    authorization_url: str = field(default="", repr=False)
    expected_state: str = field(default="", repr=False)
    error: str = ""
    tools: list[str] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    callback: asyncio.Future[AuthorizationCodeResult] | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """Return only UI state; never return codes, tokens or client credentials."""
        return {
            "flow_id": self.id,
            "name": self.name,
            "state": self.state,
            "expires_at": self.expires_at,
            "authorization_url": self.authorization_url if self.state == "authorizing" else "",
            "error": self.error,
            "tools": self.tools,
        }


class McpSignIns:
    """Own pending SDK clients until success, cancellation, expiry or application shutdown."""

    def __init__(self, on_connected: Callable[[], Awaitable[None]] | None = None) -> None:
        self._flows: dict[str, McpSignIn] = {}
        self._on_connected = on_connected

    async def start(
        self,
        name: str,
        redirect_uri: str,
        *,
        client_id: str = "",
        client_secret: str = "",
        client_metadata_url: str | None = None,
    ) -> dict[str, Any]:
        """Start only an explicit user's sign-in; resolve when the browser can open."""
        await self.cancel_name(name)
        for key, previous in list(self._flows.items()):
            if previous.expires_at <= time.time():
                await self.cancel(key)
                self._flows.pop(key, None)
        if (
            sum(bool(flow.task and not flow.task.done()) for flow in self._flows.values())
            >= MAX_PENDING
        ):
            raise ValueError("Too many sign-ins are open. Finish or cancel one first.")
        if len(self._flows) >= 32:
            for key, previous in list(self._flows.items()):
                if previous.task is None or previous.task.done():
                    self._flows.pop(key)
                    break
        spec = await asyncio.to_thread(get_user_mcp_server, name)
        if not spec or not spec.get("url"):
            raise ValueError("Save a remote MCP connection before signing in")
        flow = McpSignIn(
            id=secrets.token_urlsafe(24),
            name=name,
            revision=configuration_digest(spec),
            redirect_uri=redirect_uri,
            expires_at=time.time() + SIGN_IN_SECONDS,
        )
        flow.callback = asyncio.get_running_loop().create_future()
        self._flows[flow.id] = flow
        flow.task = asyncio.create_task(
            self._run(flow, spec, client_id, client_secret, client_metadata_url),
            name=f"mcp-sign-in:{name}",
        )
        try:
            await asyncio.wait_for(flow.ready.wait(), timeout=20)
        except TimeoutError:
            await self.cancel(flow.id)
            raise ValueError(
                "The sign-in server did not respond. Check the connection and retry."
            ) from None
        return flow.public()

    def get(self, flow_id: str) -> McpSignIn:
        """Read an exact opaque attempt; unrelated connections cannot consume its return."""
        flow = self._flows.get(flow_id)
        if flow is None:
            raise ValueError("This sign-in is no longer available. Start again.")
        return flow

    async def complete(self, name: str, flow_id: str, callback_url: str) -> dict[str, Any]:
        """Deliver a one-use, state-bound browser return to the original SDK client."""
        flow = self.get(flow_id)
        spec = await asyncio.to_thread(get_user_mcp_server, name)
        if (
            flow.name != name
            or flow.state != "authorizing"
            or flow.expires_at <= time.time()
            or not spec
            or configuration_digest(spec) != flow.revision
        ):
            raise ValueError("This sign-in no longer matches the saved connection. Start again.")
        if len(callback_url) > 16_384:
            raise ValueError("The sign-in return is invalid")
        parsed, expected = urlsplit(callback_url), urlsplit(flow.redirect_uri)
        query = parse_qs(parsed.query)
        states = query.get("state", [])
        if (
            (parsed.scheme, parsed.netloc, parsed.path)
            != (expected.scheme, expected.netloc, expected.path)
            or len(states) != 1
            or not hmac.compare_digest(states[0], flow.expected_state)
            or len(query.get("code", [])) > 1
            or len(query.get("iss", [])) > 1
            or len(query.get("error", [])) > 1
        ):
            raise ValueError("The sign-in return does not match this connection")
        if query.get("error"):
            await self.cancel(flow_id)
            flow.state, flow.error = "denied", "Sign-in was not approved. You can try again."
            return flow.public()
        if not query.get("code") or not flow.callback or flow.callback.done():
            raise ValueError("This sign-in return is missing a code or was already used")
        flow.state = "connecting"
        flow.callback.set_result(
            AuthorizationCodeResult(
                code=query["code"][0],
                state=states[0],
                iss=query.get("iss", [None])[0],
            )
        )
        return flow.public()

    async def cancel(self, flow_id: str) -> None:
        """Retire the exact pending SDK client and its transports."""
        flow = self.get(flow_id)
        if flow.task and not flow.task.done():
            flow.task.cancel()
            await asyncio.gather(flow.task, return_exceptions=True)
            flow.state = "cancelled"
        flow.authorization_url = ""

    async def cancel_name(self, name: str) -> None:
        """Settle attempts before a saved declaration is changed or removed."""
        for flow in list(self._flows.values()):
            if flow.name == name:
                await self.cancel(flow.id)

    async def close(self) -> None:
        """Retire every owned consent client during shutdown."""
        for flow_id in list(self._flows):
            await self.cancel(flow_id)
        self._flows.clear()

    async def _run(
        self,
        flow: McpSignIn,
        spec: dict[str, Any],
        client_id: str,
        client_secret: str,
        client_metadata_url: str | None,
    ) -> None:
        storage = DurableFileTokenStorage(str(spec["url"]), connection_id=flow.name)

        async def redirect(url: str) -> None:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                raise ValueError("The server returned an invalid sign-in address")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("The sign-in server must use HTTPS")
            state = parse_qs(parsed.query).get("state", [])
            if len(state) != 1 or not state[0]:
                raise ValueError("The server returned a sign-in address without state")
            flow.expected_state, flow.authorization_url = state[0], url
            flow.state = "authorizing"
            flow.ready.set()

        async def callback() -> AuthorizationCodeResult:
            assert flow.callback is not None
            return await flow.callback

        try:
            async with asyncio.timeout(SIGN_IN_SECONDS):
                metadata = managed_oauth(flow.redirect_uri)["client_metadata"]
                if client_id:
                    metadata["token_endpoint_auth_method"] = (
                        "client_secret_post" if client_secret else "none"
                    )
                    await storage.set_client_info(
                        OAuthClientInformationFull(
                            **metadata,
                            client_id=client_id,
                            client_secret=client_secret or None,
                        )
                    )
                else:
                    previous = await storage.get_client_info()
                    if previous and (
                        not previous.redirect_uris
                        or str(previous.redirect_uris[0]) != flow.redirect_uri
                    ):
                        await asyncio.to_thread(storage.clear, client_only=True)
                declaration = {
                    **spec,
                    "auth": MCPAuthConfig(
                        client_metadata=metadata,
                        storage=storage,
                        redirect_handler=redirect,
                        callback_handler=callback,
                        client_metadata_url=client_metadata_url,
                    ),
                }
                async with make_mcp_client(
                    transport_for(spec_from_declaration(flow.name, declaration)),
                    initialization_timeout=SIGN_IN_SECONDS,
                ) as client:
                    tools = await client.list_tools()
                if not storage.has_tokens():
                    raise ValueError("Authorization could not be saved")
                flow.tools = [str(tool.name) for tool in tools]
                if self._on_connected is not None:
                    await self._on_connected()
                flow.state = "connected"
        except TimeoutError:
            flow.state, flow.error = "expired", "Sign-in expired. Start again when you are ready."
        except asyncio.CancelledError:
            flow.state = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - SDK/transport failures must not disclose credentials
            logger.warning("MCP sign-in failed phase=%s kind=%s", flow.state, type(exc).__name__)
            flow.state, flow.error = (
                "failed",
                _sign_in_error(exc),
            )
        finally:
            flow.authorization_url = "" if flow.state != "authorizing" else flow.authorization_url
            flow.ready.set()
