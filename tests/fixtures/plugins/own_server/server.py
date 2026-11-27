"""A stdio MCP server shipped inside its plugin (standard library only).

One tool, ``where``, reports the plugin paths the host put in this process's
environment, so a test can tell the server was launched from the plugin's own
``mcp.json`` declaration.
"""

import json
import os
import sys

TOOLS = [
    {
        "name": "where",
        "description": "Report this server's plugin root and data directory.",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def reply(rid, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rid}
    body.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        reply(
            rid,
            {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "own-server-fixture", "version": "0.1.0"},
            },
        )
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        data = {
            "script": os.path.abspath(__file__),
            "data_dir": os.environ.get("OWN_SERVER_DATA", ""),
        }
        reply(rid, {"content": [{"type": "text", "text": json.dumps(data)}]})
    elif method == "ping":
        reply(rid, {})
    else:
        reply(rid, error={"code": -32601, "message": f"no method {method}"})
