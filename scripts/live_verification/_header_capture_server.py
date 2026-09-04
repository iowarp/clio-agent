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
says.

#1285 (C1-S5, item 1): this server now ALSO declares ``probe_with_header``, a
tool carrying an ``x-mcp-header``-ANNOTATED param (SEP-2578) -- the exerciser
gap this docstring used to record (no annotated param existed anywhere in this
repo) is closed both here and in ``tests/test_tools/mcp_exerciser.py``'s
``header_annotated_echo``. Avenue 10 now calls BOTH tools and asserts
``mcp-param-trace-id`` actually mirrors on the annotated call.

Runnable standalone::

    uv run python scripts/live_verification/_header_capture_server.py --port 18999 --log out/hcap.jsonl

Declares two tools: ``probe(payload: str) -> str`` (no annotation, the B2
baseline) and ``probe_with_header(trace_id, payload) -> str`` (B3 mirroring).
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

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

    @server.tool
    async def probe_with_header(
        trace_id: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Trace-Id"})],
        payload: str,
    ) -> str:
        """Echo ``payload``; ``trace_id`` is x-mcp-header-annotated (SEP-2578) so a
        listing client mirrors it into a ``Mcp-Param-Trace-Id`` header on call."""

        return f"probe:{trace_id}:{payload}"

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
