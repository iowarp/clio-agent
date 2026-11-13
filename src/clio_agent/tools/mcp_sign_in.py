"""The typed failure of a connection that needs the person's sign-in."""

from __future__ import annotations

from clio_agent.tools.mcp_config import MCPTransportError


class MCPSignInNeeded(MCPTransportError):
    """A connection needs the person's sign-in before CLIO can connect to it.

    ``reason`` is the typed code ``mcp_sign_in_needed``. A tool call to a connector
    never reaches this: it waits for the connector's setup card (CN5).
    """

    reason = "mcp_sign_in_needed"

    def __init__(self, connection: str) -> None:
        self.connection = connection
        super().__init__(f"mcp_sign_in_needed: {connection or 'this connection'} needs a sign-in")
