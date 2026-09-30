from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["stdio_environment"]

# A FastMCP server's start-up banner fetches pypi.org to check for updates -- a
# network round trip on every server start, between a message and its first tool.
_QUIET_SERVER_DEFAULTS = {"FASTMCP_SHOW_SERVER_BANNER": "false", "FASTMCP_CHECK_FOR_UPDATES": "off"}


def stdio_environment(spec_env: Mapping[str, str]) -> dict[str, str]:
    """Merge an MCP spec environment without leaking ambient Python overrides.

    The server starts quiet (no FastMCP banner or update check) unless its
    declaration sets those knobs itself.
    """

    env = {**os.environ, **_QUIET_SERVER_DEFAULTS, **dict(spec_env)}
    for name in ("PYTHONHOME", "PYTHONPATH"):
        if name not in spec_env:
            env.pop(name, None)
    return env
