from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

__all__ = ["stdio_environment"]

# A FastMCP server's start-up banner fetches pypi.org to check for updates -- a
# network round trip on every server start, between a message and its first tool.
_QUIET_SERVER_DEFAULTS = {"FASTMCP_SHOW_SERVER_BANNER": "false", "FASTMCP_CHECK_FOR_UPDATES": "off"}


def stdio_environment(
    spec_env: Mapping[str, str], *, command: str = "", args: Sequence[str] = ()
) -> dict[str, str]:
    """Merge an MCP spec environment without leaking ambient Python overrides.

    The server starts quiet (no FastMCP banner or update check) unless its
    declaration sets those knobs itself. The Web MCP server (``command``/``args``
    launch it) is pointed at the ``search.backend`` CLIO resolved
    (:func:`clio_agent.search.backend.web_mcp_environment`) beneath the
    declaration's own values.
    """

    from clio_agent.search.backend import web_mcp_environment  # noqa: PLC0415

    search = web_mcp_environment(command, args, spec_env) if command else {}
    env = {**os.environ, **_QUIET_SERVER_DEFAULTS, **search, **dict(spec_env)}
    for name in ("PYTHONHOME", "PYTHONPATH"):
        if name not in spec_env:
            env.pop(name, None)
    return env
