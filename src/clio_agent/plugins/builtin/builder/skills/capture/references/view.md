# View

How results should look when the person keeps asking for the same picture: a table of
runs, a chart with the lab's marking, an interactive board. Pick the lowest tier that
gives them what they asked for.

## 1. Compose what CLIO already renders (no code)

The `clio-workspace` catalog already draws tables (`clio.data-table.v1`), charts
(`clio.chart.v1`), metrics, steps, diagrams, maps, code, diffs, callouts and layout
(`Row`, `Column`, `Grid`, `Tabs`, `Card`). Load `a2ui-catalog-clio-workspace` for their
fields. Most "make it look like this" requests end here: a skill (or a fragment) that
says which components to use, with which columns, units and marks, and where to put
them. A skill's tool can return the data the view needs, so the agent fills it in
without recomputing.

A plugin may also ship a catalog under `ai.iowarp.clio/a2ui/<name>/` (`catalog.json`,
`catalog.clio.json`, `instructions.md`), but it only gives other names to components
CLIO already renders (`implements.<Name>.kernel`). A kernel CLIO does not have is
refused with `a2ui_component_unimplemented`. Do not use it to get a new look.

## 2. A view of its own: an MCP App in the plugin

When the person needs something the components cannot show (an interactive board, a
custom plot they drag, a viewer for their file type), the plugin brings its own view as
an MCP App: an MCP server in `mcp.json` whose tool names a `ui://` resource, and that
resource is one HTML page. CLIO shows it in a card in the conversation, the person can
open it in a tab, and the agent can look at it. The page runs sandboxed and talks to
CLIO only through `postMessage` (JSON-RPC 2.0): it sends `ui/initialize`, receives
`ui/notifications/tool-input` and `ui/notifications/tool-result` (the tool's
`structuredContent`), and reports its height with `ui/notifications/size-changed`.

- The tool's `_meta.ui.resourceUri` names the page; `visibility: ["model", "app"]`
  lets both the agent and the page call it.
- The resource is served with `mimeType: "text/html;profile=mcp-app"`.
- Keep the server to the standard library (as below) or to what the person's
  environment already has; a server that needs packages names its launcher (`uvx ...`).
- `mcp.json` and the server are executable parts: the plugin is asked about once
  before it first runs, like any plugin with code.

## 3. A new built-in component: a change to CLIO

When the view should be part of CLIO itself (a component every plugin can use, drawn
by the app with its own controls), no plugin can add it: it needs a new component in
CLIO's schemas, server and app. Say so plainly and offer the way:

> This needs a new kind of view in CLIO itself, which a plugin cannot add. If you give
> me access to GitHub, I'll open a pull request with the new view; it then waits for
> CLIO's maintainers to review and approve it before it reaches your app. Until then
> I'll show it with [the closest tier 1 or 2 option].

Do not open anything without that access and the person's yes. With it, follow the
`a2ui-component-design` guidance where it is available, open the pull request against
the CLIO repositories, give the person its link, and do not describe the view as
available until it is merged and released. Meanwhile the plugin ships the tier 1 or 2
version, so the person is not left waiting.

## Worked example

A lab that wants each run's peak temperatures as a board of cards, coloured by whether
the run stayed under its limit. The skill decides when to open it; the server returns
the data and the page.

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "run-board",
  "version": "0.1.0",
  "description": "Shows the lab's runs as a board of cards: peak temperature in kelvin, coloured by whether the run stayed under its limit.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```json mcp.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "runboard": {
      "type": "stdio",
      "command": "python3",
      "args": ["${PLUGIN_ROOT}/server.py"]
    }
  }
}
```

```python server.py
"""Run board: one tool that opens the board view (standard library only)."""

import json
import sys

VIEW = "ui://run-board/view"
TOOL = {
    "name": "show_runs",
    "description": "Open the run board: one card per run with its peak temperature in kelvin.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "runs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"run": {"type": "string"}, "peak_k": {"type": "number"}},
                    "required": ["run", "peak_k"],
                },
            },
            "limit_k": {"type": "number"},
        },
        "required": ["runs", "limit_k"],
    },
    "_meta": {"ui": {"resourceUri": VIEW, "visibility": ["model", "app"]}},
}
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark"><style>
body{margin:0;font:14px system-ui;color:CanvasText;background:Canvas}
main{display:flex;flex-wrap:wrap;gap:8px;padding:12px}
.card{padding:8px 12px;border-radius:6px;border:2px solid #2a7}.over{border-color:#c33}
</style></head><body><main id="board">Waiting for runs</main><script>
const send = (m) => window.parent.postMessage(m, '*');
const size = () => send({jsonrpc: '2.0', method: 'ui/notifications/size-changed',
  params: {height: document.body.scrollHeight}});
window.addEventListener('message', (e) => {
  const m = e.data;
  if (m?.id === 'init' && m.result) {
    send({jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {}});
  } else if (m?.method === 'ui/notifications/tool-result') {
    const r = m.params?.structuredContent || {};
    document.getElementById('board').replaceChildren(...(r.runs || []).map((x) => {
      const d = document.createElement('div');
      d.className = x.peak_k > r.limit_k ? 'card over' : 'card';
      d.textContent = `${x.run}: ${x.peak_k} K`;
      return d;
    }));
    size();
  }
});
send({jsonrpc: '2.0', id: 'init', method: 'ui/initialize', params: {
  protocolVersion: '2026-01-26', appInfo: {name: 'run-board', version: '0.1.0'},
  appCapabilities: {availableDisplayModes: ['inline', 'fullscreen']}}});
</script></body></html>"""


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
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
            "serverInfo": {"name": "run-board", "version": "0.1.0"},
        })
    elif method == "tools/list":
        reply(rid, {"tools": [TOOL]})
    elif method == "tools/call":
        args = params.get("arguments") or {}
        data = {"runs": args.get("runs", []), "limit_k": args.get("limit_k", 0)}
        reply(rid, {"content": [{"type": "text", "text": json.dumps(data)}],
                    "structuredContent": data})
    elif method == "resources/list":
        reply(rid, {"resources": [{"uri": VIEW, "name": "run-board",
                                   "mimeType": "text/html;profile=mcp-app"}]})
    elif method == "resources/read":
        reply(rid, {"contents": [{"uri": VIEW, "mimeType": "text/html;profile=mcp-app",
                                  "text": PAGE}]})
    else:
        reply(rid, {})
```

```markdown skills/run-board/SKILL.md
---
name: run-board
description: Show the lab's runs as a board when the person asks to see, compare or review several runs at once ("show me today's runs", "which runs went over?").
---

Read each run's peak temperature in kelvin, then call `runboard_show_runs` with every
run and the lab's limit (360 K unless the person gives another). The board appears in
the conversation; say in one line which runs went over the limit.
```
