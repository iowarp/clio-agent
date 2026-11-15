"""Probe an MCP server the way CLIO sees it, and report what its MCP Apps views need.

Connects with CLIO's own client construction (the same extensions, identity and
capabilities CLIO declares, including ``io.modelcontextprotocol/ui``), then
reports:

- the server's info, protocol version and declared capabilities;
- every tool, its parameters, and the UI resource it carries (``_meta.ui``);
- for each UI resource: its MIME type, size, CSP and permissions;
- which host features the view's code uses, found by searching its HTML for
  the MCP Apps methods and the SDK calls that send them.

The server is given as a URL or as a command:

    uv run python scripts/mcp_app_probe.py --url https://example.org/mcp
    uv run python scripts/mcp_app_probe.py --json out.json -- uvx some-server==1.0

The plan names this script ``plans/mcp-app-probe.py``; it lives here so it runs in
CLIO's environment (apps ledger, grounds). It only reads: it lists tools, reads UI
resources and calls no tool.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from typing import Any

from clio_agent.tools.mcp_config import transport_from_spec
from clio_agent.tools.mcp_runtime import make_mcp_client

#: Messages a view sends or answers (draft spec at ext-apps ea3e9d0), and the SDK
#: calls that send them. A minified bundle keeps the method strings, so the
#: method is the reliable signal; the SDK name is reported beside it.
HOST_FEATURES: dict[str, tuple[str, ...]] = {
    "ui/initialize": ("connect",),
    "ui/open-link": ("openLink",),
    "ui/download-file": ("downloadFile",),
    "ui/message": ("sendMessage",),
    "ui/update-model-context": ("updateModelContext",),
    "ui/request-display-mode": ("requestDisplayMode",),
    "ui/notifications/size-changed": ("sendSizeChanged", "autoResize"),
    "ui/notifications/host-context-changed": ("onhostcontextchanged",),
    "ui/notifications/tool-input": ("ontoolinput",),
    "ui/notifications/tool-input-partial": ("ontoolinputpartial",),
    "ui/notifications/tool-result": ("ontoolresult",),
    "ui/notifications/tool-cancelled": ("ontoolcancelled",),
    "ui/resource-teardown": ("onteardown",),
    "tools/call": ("callServerTool", "oncalltool"),
    "tools/list": ("onlisttools", "registerTool"),
    "notifications/tools/list_changed": ("sendToolListChanged",),
    "resources/read": ("readServerResource",),
    "sampling/createMessage": ("createSamplingMessage",),
    "notifications/message": ("sendLog",),
}
#: App capabilities a view may declare in ``ui/initialize``.
APP_CAPABILITY_HINTS = ("availableDisplayModes", "listChanged")
DISPLAY_MODES = ("inline", "fullscreen", "pip")


def _dump(value: Any) -> Any:
    """A JSON-safe form of an SDK model or plain value."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _ui_meta(meta: Any) -> dict[str, Any]:
    """The ``_meta.ui`` block, or the deprecated flat ``ui/resourceUri`` key."""

    meta = meta if isinstance(meta, dict) else {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    if not ui.get("resourceUri") and isinstance(meta.get("ui/resourceUri"), str):
        ui = {**ui, "resourceUri": meta["ui/resourceUri"], "flat_key": True}
    return dict(ui)


def view_features(html: str) -> dict[str, Any]:
    """Which host features a view's code refers to, by method and by SDK call."""

    used: dict[str, Any] = {}
    for method, calls in HOST_FEATURES.items():
        method_hits = len(re.findall(re.escape(f'"{method}"'), html)) + len(
            re.findall(re.escape(f"'{method}'"), html)
        )
        call_hits = {name: len(re.findall(rf"\b{re.escape(name)}\b", html)) for name in calls}
        call_hits = {name: count for name, count in call_hits.items() if count}
        if method_hits or call_hits:
            used[method] = {"method_strings": method_hits, "sdk_calls": call_hits}
    modes = sorted(mode for mode in DISPLAY_MODES if re.search(rf"['\"]{mode}['\"]", html))
    hints = sorted(hint for hint in APP_CAPABILITY_HINTS if hint in html)
    # Views written for the Apps SDK of another host call its window global instead
    # of the MCP Apps messages; CLIO answers none of those.
    other_host = sorted(set(re.findall(r"\bwindow\.openai\??\.(\w+)", html)))
    return {
        "methods": used,
        "display_mode_strings": modes,
        "capability_hints": hints,
        "other_host_api": other_host,
    }


def _script_features(scripts: list[str], csp: dict[str, Any]) -> dict[str, Any]:
    """Scan the view's own scripts too: a shell page often loads its bundle.

    Only absolute https scripts from the resource's declared ``resourceDomains`` are
    fetched, as the view's sandbox would load them.
    """

    import httpx  # noqa: PLC0415

    allowed = {str(d).rstrip("/") for d in csp.get("resourceDomains") or []}
    out: dict[str, Any] = {}
    for src in scripts:
        origin = "/".join(src.split("/", 3)[:3])
        if not src.startswith("https://") or origin not in allowed:
            out[src] = {"skipped": "not an https script from a declared resource domain"}
            continue
        try:
            response = httpx.get(src, timeout=30, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            out[src] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out[src] = {"bytes": len(response.content), "features": view_features(response.text)}
    return out


def _resource_row(uri: str, contents: list[Any]) -> dict[str, Any]:
    rows = []
    for item in contents:
        data = _dump(item)
        text = data.get("text")
        blob = data.get("blob")
        html = text if isinstance(text, str) else ""
        if not html and isinstance(blob, str):
            html = base64.b64decode(blob).decode("utf-8", "replace")
        meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
        ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
        # Script tags, and script URLs the page loads itself (a shell that injects its
        # viewer bundle at run time).
        scripts = sorted(
            set(re.findall(r"<script[^>]+src=[\"']([^\"']+)", html, flags=re.I))
            | set(re.findall(r"https://[^\"'\s)]+?\.m?js(?:\?[^\"'\s)]*)?", html))
        )
        rows.append(
            {
                "uri": data.get("uri"),
                "mime_type": data.get("mimeType"),
                "bytes": len(html.encode("utf-8")),
                "csp": ui.get("csp"),
                "permissions": ui.get("permissions"),
                "domain": ui.get("domain"),
                "prefers_border": ui.get("prefersBorder"),
                "external_scripts": scripts,
                "features": view_features(html),
                "script_features": _script_features(scripts, ui.get("csp") or {}),
            }
        )
    return {"uri": uri, "contents": rows}


async def probe(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """Connect as CLIO does, list tools, read every UI resource."""

    client = make_mcp_client(transport_from_spec(spec), server_id=name)
    async with client:
        # Era-neutral: a modern server negotiates through server/discover, with no
        # InitializeResult.
        identity = {
            "server_info": _dump(getattr(client, "server_info", None)),
            "protocol_version": getattr(client, "protocol_version", None),
            "capabilities": _dump(getattr(client, "server_capabilities", None)),
            "instructions_bytes": len((getattr(client, "instructions", None) or "").encode()),
        }
        tools = await client.list_tools()
        tool_rows = []
        uris: dict[str, list[str]] = {}
        for tool in tools:
            data = _dump(tool)
            ui = _ui_meta(data.get("_meta"))
            schema = data.get("inputSchema") or {}
            tool_rows.append(
                {
                    "name": data.get("name"),
                    "title": data.get("title"),
                    "params": sorted((schema.get("properties") or {}).keys()),
                    "required": schema.get("required") or [],
                    "ui": ui or None,
                    "meta_keys": sorted((data.get("_meta") or {}).keys()),
                }
            )
            if ui.get("resourceUri"):
                uris.setdefault(ui["resourceUri"], []).append(data["name"])
        resources = []
        for uri, owners in sorted(uris.items()):
            try:
                contents = await client.read_resource(uri)
                row = _resource_row(uri, list(contents))
            except Exception as exc:  # noqa: BLE001 -- the probe reports every failure
                row = {"uri": uri, "error": f"{type(exc).__name__}: {exc}"}
            row["tools"] = owners
            resources.append(row)
    return {
        **identity,
        "tools": tool_rows,
        "ui_resources": resources,
    }


def _summary(report: dict[str, Any]) -> str:
    lines = [f"server: {report['server_info']}  protocol {report['protocol_version']}"]
    lines.append(f"tools: {len(report['tools'])}")
    for tool in report["tools"]:
        ui = tool["ui"] or {}
        lines.append(
            f"  {tool['name']}({', '.join(tool['params'])})"
            + (f"  ui={ui.get('resourceUri')} visibility={ui.get('visibility')}" if ui else "")
        )
    for res in report["ui_resources"]:
        if "error" in res:
            lines.append(f"resource {res['uri']}: {res['error']}")
            continue
        for item in res["contents"]:
            lines.append(
                f"resource {item['uri']}: {item['mime_type']}, {item['bytes']} bytes, "
                f"csp={item['csp']}, permissions={item['permissions']}"
            )
            lines.append(f"  methods: {sorted(item['features']['methods'])}")
            lines.append(f"  display modes named: {item['features']['display_mode_strings']}")
            for src, scan in item["script_features"].items():
                if "features" not in scan:
                    lines.append(f"  script {src}: {scan}")
                    continue
                feats = scan["features"]
                lines.append(f"  script {src}: {scan['bytes']} bytes")
                lines.append(f"    methods: {sorted(feats['methods'])}")
                lines.append(f"    display modes named: {feats['display_mode_strings']}")
                if feats["other_host_api"]:
                    lines.append(f"    window.openai calls: {feats['other_host_api']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="a Streamable HTTP server")
    parser.add_argument("--name", default="probe", help="the server name CLIO would use")
    parser.add_argument("--json", help="write the full report here")
    parser.add_argument("command", nargs="*", help="a stdio server's command and arguments")
    args = parser.parse_args(argv)
    if bool(args.url) == bool(args.command):
        parser.error("give either --url or a command after --")
    if args.url:
        spec: dict[str, Any] = {"transport": "http", "url": args.url}
    else:
        spec = {"transport": "stdio", "command": args.command[0], "args": args.command[1:]}
    report = asyncio.run(probe(spec, args.name))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    print(_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
