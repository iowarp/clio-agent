"""Lazy singleton and constructors for the universal builtin tool gateway."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with a stable namespaced tool name prefix."""
    parent.mount(server, namespace=namespace)


def builtin_servers() -> tuple[tuple[str, FastMCP], ...]:
    """The in-process built-in servers, ``(namespace, server)``: the ONE list.

    ``fs``/``shell`` are universal; ``web``/``literature`` are CLIO's own search
    servers, whose tools agents receive only when a plugin claims them.
    """
    from clio_agent.tools.servers.fs_server import fs_server  # noqa: PLC0415
    from clio_agent.tools.servers.literature_server import literature_server  # noqa: PLC0415
    from clio_agent.tools.servers.shell_server import shell_server  # noqa: PLC0415
    from clio_agent.tools.servers.web_server import web_server  # noqa: PLC0415

    return (
        ("fs", fs_server),
        ("shell", shell_server),
        ("web", web_server),
        ("literature", literature_server),
    )


def _mount_builtins(gw: FastMCP) -> None:
    """Mount the in-process built-in servers onto a gateway."""
    for namespace, server in builtin_servers():
        _mount_with_namespace(gw, server, namespace)


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
