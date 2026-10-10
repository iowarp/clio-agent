"""FastMCP server implementations for CLIO Agent's universal built-in tools.

Core ships only the universal defaults (``fs``/``shell``). Every domain/case
tool is a declared MCP server connected at runtime through the declaration
mechanism, not imported here.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "fs_server",
    "shell_server",
]


def __getattr__(name: str) -> Any:
    """Load servers on demand; reading spill files must not construct tool servers."""
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        server = getattr(module, name)
        globals()[name] = server
        return server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
