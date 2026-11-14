"""A tiny stdio MCP server for the plugin-offer fixture (standard library only).

Tools: ``duty_rate`` (a fixed table of test rates) and ``landed_cost``.
"""

import json
import sys

RATES = {  # (tariff line prefix, origin) -> duty rate in percent; test values only
    ("8471", "CN"): 0.0,
    ("6109", "CN"): 12.0,
    ("6109", "BD"): 0.0,
    ("9403", "VN"): 2.7,
    ("8703", "US"): 10.0,
}

TOOLS = [
    {
        "name": "duty_rate",
        "description": "Customs duty rate (percent) for a tariff line and country of origin.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tariff_line": {"type": "string", "description": "HS code, e.g. 6109.10"},
                "origin": {"type": "string", "description": "ISO country code, e.g. CN"},
            },
            "required": ["tariff_line", "origin"],
        },
    },
    {
        "name": "landed_cost",
        "description": "Duty, VAT and total landed cost from a CIF customs value and the rates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cif_value": {"type": "number"},
                "duty_rate_percent": {"type": "number"},
                "vat_rate_percent": {"type": "number"},
            },
            "required": ["cif_value", "duty_rate_percent", "vat_rate_percent"],
        },
    },
]


def call(name, args):
    if name == "duty_rate":
        line = str(args.get("tariff_line", "")).replace(".", "")
        origin = str(args.get("origin", "")).upper()
        for (prefix, country), rate in RATES.items():
            if line.startswith(prefix) and origin == country:
                return {"tariff_line": args.get("tariff_line"), "origin": origin, "rate": rate}
        return {"error": "no_rate", "message": "No rate in this table for that line and origin."}
    if name == "landed_cost":
        cif = float(args["cif_value"])
        duty = round(cif * float(args["duty_rate_percent"]) / 100, 2)
        vat = round((cif + duty) * float(args["vat_rate_percent"]) / 100, 2)
        return {
            "cif_value": cif,
            "duty": duty,
            "vat_base": cif + duty,
            "vat": vat,
            "landed_cost": round(cif + duty + vat, 2),
        }
    raise KeyError(name)


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
        continue  # a notification
    if method == "initialize":
        reply(
            rid,
            {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tariff-fixture", "version": "0.1.0"},
            },
        )
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            data = call(params.get("name"), params.get("arguments") or {})
            reply(
                rid,
                {
                    "content": [{"type": "text", "text": json.dumps(data)}],
                    "structuredContent": data,
                    "isError": "error" in data,
                },
            )
        except Exception as exc:  # noqa: BLE001
            reply(rid, error={"code": -32602, "message": str(exc)})
    elif method == "ping":
        reply(rid, {})
    else:
        reply(rid, error={"code": -32601, "message": f"no method {method}"})
