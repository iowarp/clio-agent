#!/usr/bin/env python3
"""A minimal HTTP-transport MCP server that logs every request's headers (avenue 10).

NEW helper (no such capture server exists anywhere in this repo -- searched;
the C1-S0 exerciser is stdio-only and carries no header-capture arm). Built to
answer one narrow question live: does a tools/call reaching this server over
HTTP carry the SEP-2243/obligations-doc B2/B3 headers (``Mcp-Protocol-Version``,
``Mcp-Method``, ``Mcp-Name``, and ``Mcp-Param-{name}`` mirrored annotated
params -- see ``docs/design/mcp-client-obligations-2026-07-28.md`` rows B2/B3)?
clio_agent's own source carries ZERO code that sets any of these headers
(grepped ``src/clio_agent`` for ``Mcp-Method``/``Mcp-Param``: no hits) -- any
presence observed here is emitted by the fastmcp CLIENT LIBRARY itself, not by
clio.

A one-off scratch probe (isolated, outside this repo, not part of any run this
package makes) confirmed live that a BARE ``fastmcp.Client`` already sends
``mcp-method`` + ``mcp-protocol-version`` (and ``mcp-name`` on a ``tools/call``)
today -- B2 is "library-covered" exactly as the obligations doc's own B2 row
says. No ``mcp-param-*`` header appeared for a plain string arg: B3 mirrors
only tools with ANNOTATED header-worthy params (SEP-2578), and the exerciser
declares none -- so B3 is untestable with today's tool matrix regardless of
what the live run finds (a second, separate exerciser gap from the ones
``mcp_exerciser.py`` already documents). ``leg_c2_v2_avenues.py``'s avenue 10
drives clio's REAL wrapped client (``tools/mcp_runtime.py::make_mcp_client``,
via ``POST /v1/mcp/servers/{sid}/call`` -- see that leg's docstring for why
this headless REST-install-lane surface is representative here even though it
is NOT the declared/gateway path) against a running instance of this server
and reads back what actually landed, rather than assuming either outcome.

Runnable standalone::

    uv run python scripts/live_verification/_header_capture_server.py --port 18999 --log out/hcap.jsonl

Declares one tool, ``probe(payload: str) -> str``, returning ``f"probe:{payload}"``.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def build_capture_app(log_path: Path) -> Any:
    """Build the FastMCP HTTP ASGI app with a header-capturing middleware mounted.

    Every request (``server/discover``/``initialize``, ``tools/list``,
    ``tools/call``, ...) appends one JSON line to ``log_path``: ``{"method",
    "path", "headers"}`` -- ``headers`` is the FULL raw header mapping
    (lower-cased keys, per Starlette/ASGI convention) so a caller can grep for
    any of ``mcp-method`` / ``mcp-name`` / ``mcp-protocol-version`` /
    ``mcp-param-*`` after the fact, without this module needing to know the
    exact header catalog in advance.
    """

    from fastmcp import FastMCP
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    server = FastMCP("hcap")

    @server.tool
    async def probe(payload: str) -> str:
        """Echo ``payload`` back -- the ONLY point is what headers arrived with the call."""

        return f"probe:{payload}"

    class _HeaderCaptureMiddleware(BaseHTTPMiddleware):
        def __init__(self, app: Any, *, log_path: Path) -> None:
            super().__init__(app)
            self._log_path = log_path

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            row = {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
            line = json.dumps(row, default=str)
            with _LOCK:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.write("\n")
            return await call_next(request)

    return server.http_app(middleware=[Middleware(_HeaderCaptureMiddleware, log_path=log_path)])


def read_captured_rows(log_path: Path) -> list[dict[str, Any]]:
    """Read back every captured request row (best-effort: missing file -> empty)."""

    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", required=True, help="path to the captured-headers JSONL log")
    parser.add_argument("--host", default="127.0.0.1")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    app = build_capture_app(log_path)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
