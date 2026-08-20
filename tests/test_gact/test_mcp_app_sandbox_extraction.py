"""Symbol-parity tests for the extracted MCP App sandbox boundary."""

from __future__ import annotations

from clio_agent.gact import mcp_app_sandbox, mcp_apps


def test_mcp_app_sandbox_symbols_are_reexported() -> None:
    """The legacy module must re-export the extracted sandbox objects."""

    moved_names = (
        "_host_origin",
        "_request_origin",
        "_alternate_loopback_origin",
        "_sandbox_url",
        "_safe_sources",
        "_csp_header",
        "_SANDBOX_DOCUMENT",
    )

    for name in moved_names:
        assert getattr(mcp_apps, name) is getattr(mcp_app_sandbox, name)
