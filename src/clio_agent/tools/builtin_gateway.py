"""Lazy singleton and constructors for the universal builtin tool gateway."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with a stable namespaced tool name prefix."""
    parent.mount(server, namespace=namespace)


def _mount_builtins(gw: FastMCP) -> None:
    """Mount the universal in-process built-in servers onto a gateway."""
    from clio_agent.tools.servers.fs_server import fs_server  # noqa: PLC0415
    from clio_agent.tools.servers.shell_server import shell_server  # noqa: PLC0415

    _mount_with_namespace(gw, fs_server, "fs")
    _mount_with_namespace(gw, shell_server, "shell")


def _new_base_gateway() -> FastMCP:
    """Return a fresh gateway with only the universal built-ins mounted."""
    from fastmcp import FastMCP  # noqa: PLC0415

    gw = FastMCP("clio-gateway")
    _mount_builtins(gw)
    return gw


# Gateway singleton: the universal built-ins only. Declared domain servers are
# mounted per agent via ``build_gateway(load_mcp_servers(...))``.
_gateway: FastMCP | None = None
_gateway_lock = Lock()


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance (built-ins only)."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = _new_base_gateway()
        return _gateway
