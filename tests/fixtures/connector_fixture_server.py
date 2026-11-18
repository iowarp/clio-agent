"""Synthetic MCP servers behind the fixture connectors (tests/fixtures/connectors).

``--mode plain``: one tool, nothing to set up.
``--mode secret``: reads ``FIXTURE_TOKEN`` and ``FIXTURE_WHERE`` from its environment
and reports only a short hash of the token, never the token, so a check can prove
the server received it without the value reaching the model or a transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Any

from fastmcp import FastMCP


def application(mode: str) -> FastMCP:
    mcp = FastMCP(f"Connector fixture ({mode})")
    if mode == "plain":

        @mcp.tool()
        def fixture_ping(text: str = "ping") -> dict[str, Any]:
            """Echo a word back from the plain fixture connector (no external effects)."""
            return {"echo": text, "server": "fixture-plain"}

    else:

        @mcp.tool()
        def fixture_secret_check() -> dict[str, Any]:
            """Report whether this fixture server received its access token, and where it runs."""
            token = os.environ.get("FIXTURE_TOKEN", "")
            return {
                "received": bool(token),
                "token_sha256_prefix": hashlib.sha256(token.encode()).hexdigest()[:12]
                if token
                else "",
                "where": os.environ.get("FIXTURE_WHERE") or "this machine",
                "pid": os.getpid(),
            }

    return mcp


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("plain", "secret"), default="plain")
    application(parser.parse_args().mode).run(transport="stdio", show_banner=False)
