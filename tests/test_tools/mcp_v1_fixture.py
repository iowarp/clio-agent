"""A FROZEN v1-era MCP server: hand-rolled stdio JSON-RPC at 2025-11-25.

The dual-era conformance bed's legacy half (C1-S0, iowarp/clio-agent#1280).
fastmcp 4 cannot be made to refuse the 2026-07-28 era -- it serves modern and
merely tolerates legacy fronts -- so a GENUINE legacy-only server must be
hand-rolled. This one answers ``initialize`` with ``2025-11-25`` no matter
what the client proposes (the classic downgrade negotiation), serves a plain
no-tasks tool over the legacy camelCase wire, and answers every unknown
method -32601 so an auto-mode client's modern probing falls back cleanly.

It also converts ``test_protocol_compat_matrix.py``'s recorded-prose legacy
legs into executed ones: the campaign's "genuine v1 server" for byte-identical
regression and for C1-S1's renamed suppression-reason arm.

Pure stdlib on purpose: the spawned subprocess needs no venv imports at all.
Import-only as a test helper (constants below); run directly for the server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

V1_FIXTURE_PATH = Path(__file__).resolve()
V1_PROTOCOL_VERSION = "2025-11-25"
V1_SERVER_NAME = "v1fix"
V1_TOOL_NAME = "legacy_echo"

_TOOL_ROW: dict[str, Any] = {
    "name": V1_TOOL_NAME,
    "description": "Echo on the frozen 2025-11-25 wire (no tasks, camelCase schema).",
    "inputSchema": {
        "type": "object",
        "properties": {"payload": {"type": "string"}},
        "required": ["payload"],
    },
}


def _reply(request_id: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def _reply_error(request_id: Any, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )
        + "\n"
    )
    sys.stdout.flush()


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return  # notifications (initialized, cancelled, ...) are absorbed
    if method == "initialize":
        _reply(
            request_id,
            {
                "protocolVersion": V1_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": V1_SERVER_NAME, "version": "0.0.1"},
            },
        )
    elif method == "ping":
        _reply(request_id, {})
    elif method == "tools/list":
        _reply(request_id, {"tools": [_TOOL_ROW]})
    elif method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}
        if params.get("name") != V1_TOOL_NAME:
            _reply_error(request_id, -32602, f"unknown tool: {params.get('name')!r}")
            return
        _reply(
            request_id,
            {
                "content": [{"type": "text", "text": f"legacy:{arguments.get('payload')}"}],
                "isError": False,
            },
        )
    else:
        # Everything modern (server/discover, subscriptions/listen, tasks/*)
        # does not exist in this era: refuse by method so auto-mode clients
        # fall back to the legacy handshake instead of hanging.
        _reply_error(request_id, -32601, f"method not found: {method!r}")


def main() -> None:
    """Serve newline-delimited JSON-RPC on stdio until EOF."""

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            _handle(message)


if __name__ == "__main__":
    main()
