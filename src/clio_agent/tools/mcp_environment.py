from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

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
    declaration's own values, with a writable state directory under CLIO's data
    root (:func:`clio_agent.search.web_mcp_default.web_mcp_state_dir`).
    """

    from clio_agent.search.backend import is_web_search_mcp, web_mcp_environment  # noqa: PLC0415
    from clio_agent.search.web_mcp_default import web_mcp_state_dir  # noqa: PLC0415

    search = web_mcp_environment(command, args, spec_env) if command else {}
    if command and is_web_search_mcp(command, args) and "WEB_STATE_DIR" not in spec_env:
        search["WEB_STATE_DIR"] = str(web_mcp_state_dir())
    from clio_agent.tools.mcp_prepare import uv_project_environment  # noqa: PLC0415

    # A ``uv run --project`` server runs from CLIO's environment, never ``<project>/.venv``.
    project_env = uv_project_environment(command, args, spec_env) if command else {}
    env = {**os.environ, **_QUIET_SERVER_DEFAULTS, **search, **project_env, **dict(spec_env)}
    launcher = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if launcher.removesuffix(".exe").removesuffix(".cmd") == "clio-kit":
        from clio_agent import paths  # noqa: PLC0415 - resolve this instance's private cache

        # Reuse the same short content-addressed runtime cache as Desktop.
        # Nesting projects beneath the redirected Windows profile adds another
        # workspace hash and exceeds MAX_PATH while copying locked projects.
        if not env.get("CLIO_KIT_CACHE_DIR"):
            env["CLIO_KIT_CACHE_DIR"] = str(paths.user_cache_dir() / "mcp-runtime")
        from clio_agent.tools.mcp_environment_relocation import (  # noqa: PLC0415
            remove_stale_environments,
        )

        # An environment built at another location (a moved cache) is rebuilt, never run.
        remove_stale_environments(Path(env["CLIO_KIT_CACHE_DIR"]))
    for name in ("PYTHONHOME", "PYTHONPATH"):
        if name not in spec_env:
            env.pop(name, None)
    return env
