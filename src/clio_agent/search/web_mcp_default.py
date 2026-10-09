"""The Web MCP server CLIO declares itself, so ``web_search`` works without a manual step.

When ``search.backend`` resolves to a backend that can serve (``local_searxng``, the
default, or ``clio_web_search``), the main agent gets the Web MCP server
(``clio-kit mcp-server web``) as namespace ``web`` without any declaration, and it
loads for the root session by default. A person's own Web MCP declaration (any name,
any scope) replaces this one, and it loads by default too, because a Web MCP server
that only a blueprint can mount leaves the main agent without ``web_search``. With
``search.backend: none``, nothing is declared. :func:`web_mcp_state_dir` is the
writable state directory every Web MCP spawn gets: its own default
(``~/.config/clio-kit``) is not writable under CLIO's tool sandbox.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from clio_agent.search.backend import WEB_SEARCH_TOOL, is_web_search_mcp, resolve_search_backend
from clio_agent.search.settings import SearchConfigurationError, SearchSettings
from clio_agent.tools.mcp_config import MCPServerSpec, load_mcp_servers

logger = logging.getLogger(__name__)

DEFAULT_WEB_MCP_NAME = "web"
DEFAULT_WEB_MCP_SOURCE = "clio-default"
_LAUNCHER = "clio-kit"
_ARGS = ("mcp-server", "web")


def web_mcp_state_dir() -> Path:
    """The Web MCP server's state directory under CLIO's own data root."""

    from clio_agent import paths  # noqa: PLC0415 - paths reads the environment late

    return paths.user_data_dir() / "web-mcp"


def _launcher_available() -> bool:
    from clio_agent.tools.desktop_mcp_runtime import bundled_module_launcher  # noqa: PLC0415

    return bool(shutil.which(_LAUNCHER)) or bundled_module_launcher(_LAUNCHER, []) is not None


def _is_web_mcp(spec: MCPServerSpec) -> bool:
    return spec.transport == "stdio" and is_web_search_mcp(spec.command, spec.args)


def with_default_web_mcp(
    specs: Mapping[str, MCPServerSpec], *, settings: SearchSettings | None = None
) -> dict[str, MCPServerSpec]:
    """``specs`` plus CLIO's Web MCP declaration, every Web MCP server loading by default."""

    merged = {
        name: replace(spec, always_load=True) if _is_web_mcp(spec) else spec
        for name, spec in specs.items()
    }
    if DEFAULT_WEB_MCP_NAME in merged or any(_is_web_mcp(spec) for spec in merged.values()):
        return merged
    try:
        backend = resolve_search_backend(settings)
    except SearchConfigurationError as exc:
        logger.error("web mcp not declared reason=%s: %s", exc.code, exc)
        return merged
    if backend.name == "none":
        return merged
    if not _launcher_available():
        logger.warning(
            "web mcp not declared reason=launcher_missing: %s is not on PATH "
            "(uv tool install clio-kit, or set search.backend: none)",
            _LAUNCHER,
        )
        return merged
    merged[DEFAULT_WEB_MCP_NAME] = MCPServerSpec(
        name=DEFAULT_WEB_MCP_NAME,
        transport="stdio",
        command=_LAUNCHER,
        args=_ARGS,
        always_load=True,
        source=DEFAULT_WEB_MCP_SOURCE,
    )
    return merged


def load_agent_mcp_servers(**kwargs: Any) -> dict[str, MCPServerSpec]:
    """:func:`load_mcp_servers` as the agent sees it: with the Web MCP declaration."""

    return with_default_web_mcp(load_mcp_servers(**kwargs))


def degraded_web_mcp_placeholder(namespace: str, spec: MCPServerSpec | None) -> dict[str, Any]:
    """A stand-in ``web_search`` while the Web MCP namespace has not listed its tools.

    The Web MCP server cannot start while its backend is not ready (a CLIO Web Search
    gateway still starting fails the server's own startup discovery), so discovery
    marks namespace ``web`` degraded and, without this, the agent has no
    ``web_search`` at all. The stand-in keeps the tool present: CLIO's guard
    (:func:`clio_agent.search.backend.web_search_guard`) answers its calls with the
    typed ``search_backend_unavailable`` error naming the fix, and the namespace
    healer replaces it with the real tools once the server lists them (same key).
    ``{}`` for any other namespace.
    """

    if namespace != DEFAULT_WEB_MCP_NAME or spec is None or not _is_web_mcp(spec):
        return {}
    from mcp.types import Tool  # noqa: PLC0415 - the MCP SDK is heavy; only on degrade

    name = WEB_SEARCH_TOOL
    tool = Tool(
        name=name,
        description=(
            "Search the web. The search backend is not ready yet; a call returns "
            "what to do, and the full tool appears once the backend serves."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "count": {"type": "integer", "description": "Maximum results to return."},
            },
            "required": ["query"],
        },
    )
    return {name: tool}
