"""A hand-rolled, deliberately MUST-violating MCP fixture (#1285, C1-S5 item 4).

fastmcp builds a structurally-correct wire (initialize/server/discover
handshake, standard headers, JSON-RPC envelope). Reimplementing the whole
2026-07-28 HTTP transport by hand just to break four response bodies would be
its own large, bug-prone surface, so this fixture wraps a real fastmcp app in
a PURE ASGI middleware (deliberately not ``BaseHTTPMiddleware`` -- its
concurrent disconnect-watcher task races a body-replay wrapper and corrupts
the handshake) that peeks each request's JSON-RPC method/tool name and, for
four specific tool names, SHORT-CIRCUITS the response entirely -- answering
with a hand-built, deliberately malformed JSON-RPC frame instead of ever
reaching fastmcp for that call. Every other request (the handshake,
``tools/list``'s first page, any other tool call) passes through untouched,
so a real client completes negotiation normally -- this is the
"bypassing fastmcp's own protocol correctness" LEG_C2.md's adversarial gap
asked for, scoped to exactly the responses that need to be wrong.

The four violations:

- ``bad_result_type``: a ``tools/call`` result carries an UNRECOGNIZED
  ``resultType`` (SEP-2322/obligations A2: absent -> "complete", unrecognized
  -> invalid, "input_required" -> MRTR).
- ``bad_missing_caps``: a ``-32021 MissingRequiredClientCapability`` error
  whose ``data`` carries NO ``requiredCapabilities`` key (the field the spec
  requires for the error to be actionable -- clio's typed mapping must not
  crash on its absence).
- ``bad_header_mismatch``: EVERY call answers ``-32020 HeaderMismatch``,
  regardless of headers actually matching -- a hostile server that never
  resolves, proving ``tools/mcp_header_mismatch.py``'s retry is bounded
  (exactly one retry, never a loop) even against a server that always refuses.
- pagination: ``tools/list`` returns an EMPTY-STRING ``nextCursor`` on its
  first page (E10: only null/missing ends -- an empty string is a valid,
  non-terminal cursor) so a client that mistakes falsy-string-means-done for
  the spec's actual rule stops one page early; the SECOND page (cursor="")
  carries the remaining tool and no ``nextCursor``.

Servable standalone (``python mcp_adversarial_fixture.py --port PORT``) as a
second declared MCP server; ``run_adversarial_lifespan`` below drives it
in-process over a real HTTP transport for tests (fastmcp's ASGI app requires
a running lifespan for its session manager -- a bare ASGI-transport call
without it raises at connect time).
"""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from typing import Any

__all__ = [
    "BAD_HEADER_MISMATCH_TOOL",
    "BAD_MISSING_CAPS_TOOL",
    "BAD_RESULT_TYPE_TOOL",
    "PAGINATED_TOOL",
    "PAGINATED_TOOL_2",
    "adversarial_in_process_transport",
    "build_adversarial_app",
    "run_adversarial_lifespan",
]

BAD_RESULT_TYPE_TOOL = "bad_result_type"
BAD_MISSING_CAPS_TOOL = "bad_missing_caps"
BAD_HEADER_MISMATCH_TOOL = "bad_header_mismatch"
PAGINATED_TOOL = "paginated_a"
PAGINATED_TOOL_2 = "paginated_b"


def _ok_result(request_id: Any, text: str, *, result_type: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        # A str-returning @server.tool carries an inferred output schema, so the
        # SDK client's validate_tool_result rejects a response with no
        # structuredContent BEFORE resultType is ever examined -- matching
        # fastmcp's own shape for a plain str return keeps that check happy.
        "structuredContent": {"result": text},
        "isError": False,
    }
    if result_type is not None:
        result["resultType"] = result_type
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _missing_caps_error(request_id: Any) -> dict[str, Any]:
    # A well-formed -32021 per SEP-1686 always carries data.requiredCapabilities;
    # this omits it entirely (an empty data object) -- the violation.
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32021, "message": "missing required client capability", "data": {}},
    }


def _header_mismatch_error(request_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32020, "message": "header mismatch (adversarial: always fires)"},
    }


#: ``ListToolsResult`` is a ``CacheableResult`` on the 2026-07-28 wire model:
#: cacheScope/ttlMs/resultType are REQUIRED fields (no default), always
#: stamped by a real server even with no cache hint configured (ttlMs=0 is
#: the "don't cache" wire value, not an absent field) -- present on every
#: hand-built response below so ONLY the cursor value is the deliberate
#: violation under test.
_NO_CACHE_HINT_FIELDS: dict[str, Any] = {
    "cacheScope": "private",
    "ttlMs": 0,
    "resultType": "complete",
}


def _paginated_list_result(request_id: Any, *, cursor: Any) -> dict[str, Any]:
    first_page = {
        "tools": [
            {
                "name": PAGINATED_TOOL,
                "description": "first page",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        "nextCursor": "",  # E10: empty string is a VALID, non-terminal cursor
        **_NO_CACHE_HINT_FIELDS,
    }
    second_page = {
        "tools": [
            {
                "name": PAGINATED_TOOL_2,
                "description": "second (final) page",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        # no nextCursor: only null/missing ends pagination
        **_NO_CACHE_HINT_FIELDS,
    }
    return {"jsonrpc": "2.0", "id": request_id, "result": second_page if cursor == "" else first_page}


class _MutatingASGIMiddleware:
    """Pure ASGI (not ``BaseHTTPMiddleware`` -- see module docstring) request
    inspector + selective short-circuit responder."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(chunks)

        replayed = False

        async def receive_wrapper() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        short_circuit = self._short_circuit_response(body)
        if short_circuit is not None:
            payload = json.dumps(short_circuit).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        await self.app(scope, receive_wrapper, send)

    @staticmethod
    def _short_circuit_response(body: bytes) -> dict[str, Any] | None:
        try:
            req = json.loads(body)
        except (ValueError, TypeError):
            return None
        if not isinstance(req, dict):
            return None
        method = req.get("method")
        request_id = req.get("id")

        if method == "tools/call":
            tool_name = (req.get("params") or {}).get("name")
            if tool_name == BAD_RESULT_TYPE_TOOL:
                return _ok_result(request_id, "bad-result-type", result_type="totally-bogus-result-type")
            if tool_name == BAD_MISSING_CAPS_TOOL:
                return _missing_caps_error(request_id)
            if tool_name == BAD_HEADER_MISMATCH_TOOL:
                return _header_mismatch_error(request_id)
            return None

        if method == "tools/list":
            # BOTH pages are short-circuited (never delegated to fastmcp): the
            # first, cursor-less page must inject the empty-string nextCursor
            # itself, so it cannot pass through to the real handler either.
            cursor = (req.get("params") or {}).get("cursor")
            return _paginated_list_result(request_id, cursor=cursor)

        return None


def build_adversarial_app(*, name: str = "adversarial") -> Any:
    """Build the ASGI app: a genuinely-correct handshake, four deliberate violations."""

    from fastmcp import FastMCP

    server = FastMCP(name)

    @server.tool
    async def bad_result_type(payload: str) -> str:
        """Never actually reached: the middleware short-circuits every call."""

        return f"echo:{payload}"  # pragma: no cover

    @server.tool
    async def bad_missing_caps(payload: str) -> str:
        """Never actually reached: the middleware short-circuits every call."""

        return f"echo:{payload}"  # pragma: no cover

    @server.tool
    async def bad_header_mismatch(payload: str) -> str:
        """Never actually reached: the middleware short-circuits every call."""

        return f"echo:{payload}"  # pragma: no cover

    app = server.http_app()
    return _MutatingASGIMiddleware(app)


@asynccontextmanager
async def run_adversarial_lifespan(app: Any):  # noqa: ANN201
    """Drive ``app``'s ASGI lifespan manually (startup then shutdown).

    fastmcp's streamable-HTTP session manager initializes its task group during
    the ASGI ``lifespan.startup`` event -- a bare in-process ASGI transport
    call (``httpx2.ASGITransport``) never sends that event on its own, so a
    connect through it fails ("task group was not initialized") without this.
    """

    import asyncio

    startup_complete = asyncio.Event()
    shutdown_complete = asyncio.Event()
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await receive_queue.put({"type": "lifespan.startup"})

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            startup_complete.set()
        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await startup_complete.wait()
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        await shutdown_complete.wait()
        await task


def adversarial_in_process_transport(app: Any) -> Any:
    """A ``fastmcp.client.transports.http.StreamableHttpTransport`` wired to an
    in-process ``httpx2.ASGITransport`` against ``app`` -- no real socket, no
    subprocess. Caller is responsible for running ``app`` under
    :func:`run_adversarial_lifespan`."""

    import httpx2
    from fastmcp.client.transports.http import StreamableHttpTransport

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("timeout", None)
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://adversarial.local",
            **kwargs,
        )

    return StreamableHttpTransport("http://adversarial.local/mcp", httpx_client_factory=factory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    import uvicorn

    uvicorn.run(build_adversarial_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
