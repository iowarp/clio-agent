"""CLIO tool gateway exports.

The gateway mounts the universal in-process built-ins (filesystem, shell)
and proxy-mounts any declared MCP servers next to them. Keep these imports
lazy so lightweight modules, including the GACT health/capabilities server,
can import file-policy helpers without paying the full gateway startup cost.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["gateway", "get_gateway", "list_gateway_tools"]


def __getattr__(name: str) -> Any:
    """Load gateway exports on first use instead of package import."""

    if name in __all__:
        gateway_module = importlib.import_module("clio_agent.tools.gateway")
        return getattr(gateway_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
