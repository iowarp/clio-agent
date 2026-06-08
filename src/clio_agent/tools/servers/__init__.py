"""FastMCP server implementations for CLIO Agent's universal built-in tools.

Core ships only the universal defaults (``fs``/``shell``). Every domain/case
tool is a declared MCP server connected at runtime through the declaration
mechanism, not imported here.
"""

from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.shell_server import shell_server

__all__ = [
    "fs_server",
    "shell_server",
]
