"""Symbol-parity tests for the extracted MCP App sandbox boundary."""

from __future__ import annotations

from clio_agent.gact import mcp_app_sandbox, mcp_apps


def test_mcp_app_sandbox_symbols_are_reexported() -> None:
    """The legacy module must re-export the extracted sandbox objects."""

    moved_names = (
        "_host_origin",
        "_request_origin",
        "_sandbox_url",
        "_safe_sources",
        "_csp_header",
        "_SANDBOX_DOCUMENT",
    )

    for name in moved_names:
        assert getattr(mcp_apps, name) is getattr(mcp_app_sandbox, name)


def test_a_view_may_evaluate_code_but_reach_only_what_it_declared() -> None:
    """MC-D1: scripts may evaluate code, as in the reference host; sources stay declared."""

    csp = mcp_app_sandbox._csp_header(
        {"resourceDomains": ["https://*.tiles.example"], "connectDomains": ["https://api.example"]},
        "http://127.0.0.1:5173",
    )
    script = next(part for part in csp.split("; ") if part.startswith("script-src "))
    assert script == (
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: https://*.tiles.example"
    )
    assert "connect-src 'self' https://api.example" in csp
    assert "object-src 'none'" in csp and "frame-ancestors http://127.0.0.1:5173" in csp
    # A declared source cannot smuggle a keyword in.
    smuggled = mcp_app_sandbox._csp_header({"resourceDomains": ["'unsafe-hashes'"]}, "x")
    assert "unsafe-hashes" not in smuggled


def test_the_sandbox_tells_the_host_when_a_view_outgrows_its_frame() -> None:
    """#70: wrapped text inside a fixed-height view must reach the host, not be cut."""

    document = mcp_app_sandbox._SANDBOX_DOCUMENT
    assert "@@" not in document
    assert mcp_app_sandbox.CONTENT_HEIGHT_METHOD == "clio/notifications/view-content-height"
    assert mcp_app_sandbox.CONTENT_HEIGHT_METHOD in document
    assert "ResizeObserver" in document and "MutationObserver" in document
