# App (MCP App)

A view that belongs to another application or system: one that owns the data and its
behaviour, such as an observing program's data service, an instrument's controller or
a lab system with its own records and actions. CLIO does not copy that data into a file
to draw it; the system's MCP server shows it and answers the view's requests. For data
CLIO holds (the person's files, results it computed), use an A2UI surface instead
(`references/view.md`), and for a widget CLIO lacks, a change to CLIO through a pull
request (same reference).

If the system already has an MCP server or a connector, the plugin uses it through a
named connection (`references/connection.md`) and only adds the skill; it writes a
server only when none exists.

## How an MCP App works

- An MCP server in the plugin's `mcp.json` talks to the system. One of its tools names
  a `ui://` resource in `_meta.ui.resourceUri`; CLIO shows that page in a card in the
  conversation, the person can open it in a tab, and the agent can look at it.
- The resource is one HTML page served with `mimeType: "text/html;profile=mcp-app"`.
  It runs sandboxed and talks to CLIO only through `postMessage` (JSON-RPC 2.0): it
  sends `ui/initialize`, receives `ui/notifications/tool-result` (the opening tool's
  `structuredContent`), reports its height with `ui/notifications/size-changed`, and
  asks the system for more through `tools/call` on its own server's tools.
- A tool only the page calls (more detail when the person clicks) has
  `visibility: ["app"]`; the opening tool has `["model", "app"]`.
- What the person picks is reported with `ui/update-model-context`, so the agent knows
  what they are looking at.
- The system's address goes in the server's `env` in `mcp.json`; credentials go in the
  connection, never in the plugin. `mcp.json` and the server are executable parts: the
  plugin is asked about once before it first runs.

## Worked example

An oceanographic program runs a fleet of profiling floats; its data service at
`FLEET_API` lists the floats and serves each float's latest temperature profile. The
team wants its own view in their conversations: a map of the fleet, and the profile of
a float when they click it, fetched from the program at that moment.

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "float-fleet",
  "version": "0.1.0",
  "description": "The float program's own view: the fleet on a map and, on a click, a float's latest temperature profile from the program's data service.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```json mcp.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "fleet": {
      "type": "stdio",
      "command": "python3",
      "args": ["${PLUGIN_ROOT}/server.py"],
      "env": {"FLEET_API": "https://floats.example-ocean.org/api/v1"}
    }
  }
}
```

```python server.py
"""The float program's view: the fleet and a float's profile, from its data service."""

import json
import os
import sys
import urllib.parse
import urllib.request

API = os.environ.get("FLEET_API", "").rstrip("/")
VIEW = "ui://float-fleet/view"
TOOLS = [
    {
        "name": "show_fleet",
        "description": "Open the program's fleet view: every float on a map, a region optional.",
        "inputSchema": {"type": "object", "properties": {"region": {"type": "string"}}},
        "_meta": {"ui": {"resourceUri": VIEW, "visibility": ["model", "app"]}},
    },
    {
        "name": "float_profile",
        "description": "One float's latest temperature profile (depth in m, temperature in degC).",
        "inputSchema": {
            "type": "object",
            "properties": {"float_id": {"type": "string"}},
            "required": ["float_id"],
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
]
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark"><style>
body{margin:0;font:13px system-ui;color:CanvasText;background:Canvas}
main{display:grid;grid-template-columns:1fr 220px;gap:10px;padding:10px}
svg{width:100%;height:260px;border:1px solid GrayText;border-radius:6px}
circle{cursor:pointer}circle.picked{stroke:CanvasText;stroke-width:2}
</style></head><body><main><svg id="map" viewBox="-180 -90 360 180"></svg>
<div><h3 id="title">Pick a float</h3><svg id="profile" viewBox="0 0 220 260"></svg></div>
</main><script>
let next = 1; const waiting = {};
const send = (m) => window.parent.postMessage(m, '*');
const request = (method, params) => new Promise((ok, fail) => {
  const id = 'r' + next++; waiting[id] = {ok, fail}; send({jsonrpc: '2.0', id, method, params});
});
const size = () => send({jsonrpc: '2.0', method: 'ui/notifications/size-changed',
  params: {height: document.body.scrollHeight}});
const svg = (tag, attrs) => Object.assign(
  document.createElementNS('http://www.w3.org/2000/svg', tag), attrs);
async function pick(f, dot) {
  document.querySelectorAll('circle').forEach((c) => c.classList.remove('picked'));
  dot.classList.add('picked');
  document.getElementById('title').textContent = 'Float ' + f.id + ': loading';
  const result = await request('tools/call', {name: 'float_profile', arguments: {float_id: f.id}});
  const p = result.structuredContent || {levels: []};
  const plot = document.getElementById('profile'); plot.replaceChildren();
  const max = Math.max(1, ...p.levels.map((l) => l.depth_m));
  const temps = p.levels.map((l) => l.temp_c), lo = Math.min(...temps), hi = Math.max(...temps);
  const points = p.levels.map((l) =>
    `${10 + 200 * (l.temp_c - lo) / Math.max(hi - lo, 0.1)},${10 + 240 * l.depth_m / max}`);
  plot.append(svg('polyline', {}));
  plot.lastChild.setAttribute('points', points.join(' '));
  plot.lastChild.setAttribute('fill', 'none'); plot.lastChild.setAttribute('stroke', 'currentColor');
  document.getElementById('title').textContent =
    `Float ${f.id}: ${lo.toFixed(1)} to ${hi.toFixed(1)} degC, 0 to ${max} m`;
  request('ui/update-model-context', {content: [{type: 'text',
    text: `The person is looking at float ${f.id}'s latest profile (${p.time || 'time unknown'}).`}]});
  size();
}
window.addEventListener('message', (e) => {
  const m = e.data;
  if (m?.id && waiting[m.id]) {
    const w = waiting[m.id]; delete waiting[m.id];
    return m.error ? w.fail(m.error) : w.ok(m.result);
  }
  if (m?.method === 'ui/notifications/tool-result') {
    const map = document.getElementById('map'); map.replaceChildren();
    for (const f of m.params?.structuredContent?.floats || []) {
      const dot = svg('circle', {}); dot.setAttribute('cx', f.lon); dot.setAttribute('cy', -f.lat);
      dot.setAttribute('r', 3); dot.setAttribute('fill', f.active ? '#2a7' : 'GrayText');
      dot.addEventListener('click', () => pick(f, dot)); map.append(dot);
    }
    size();
  }
});
request('ui/initialize', {protocolVersion: '2026-01-26',
  appInfo: {name: 'float-fleet', version: '0.1.0'},
  appCapabilities: {availableDisplayModes: ['inline', 'fullscreen']}})
  .then(() => { send({jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {}}); size(); });
</script></body></html>"""


def fetch(path, **query):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def call(name, args):
    if name == "show_fleet":
        region = args.get("region") or ""
        return {"floats": fetch("/floats", **({"region": region} if region else {}))}
    if name == "float_profile":
        return fetch("/floats/" + urllib.parse.quote(str(args["float_id"])) + "/profile")
    raise KeyError(name)


def reply(rid, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})}
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    msg = json.loads(raw) if raw.strip() else {}
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        reply(rid, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "float-fleet", "version": "0.1.0"},
        })
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            data = call(params.get("name"), params.get("arguments") or {})
        except (OSError, ValueError, KeyError) as exc:
            reply(rid, {"content": [{"type": "text", "text": f"The program's service: {exc}"}],
                        "isError": True})
            continue
        reply(rid, {"content": [{"type": "text", "text": json.dumps(data)}],
                    "structuredContent": data if isinstance(data, dict) else {"items": data}})
    elif method == "resources/list":
        reply(rid, {"resources": [{"uri": VIEW, "name": "float-fleet",
                                   "mimeType": "text/html;profile=mcp-app"}]})
    elif method == "resources/read":
        reply(rid, {"contents": [{"uri": VIEW, "mimeType": "text/html;profile=mcp-app",
                                  "text": PAGE}]})
    else:
        reply(rid, error={"code": -32601, "message": f"no method {method}"})
```

```markdown skills/float-fleet/SKILL.md
---
name: float-fleet
description: Show the float program's fleet or a float's profile ("where are our floats?", "show the fleet in the Labrador Sea", "what did float 4903 measure?").
---

Call `fleet_show_fleet` (with `region` when the person names one). The program's own
view opens in the conversation: the fleet on a map, and a float's latest profile when
the person clicks it, fetched from the program at that moment. Say in one line how many
floats are active. When the person asks about the float they picked, the view has
told you which one it is.
```
