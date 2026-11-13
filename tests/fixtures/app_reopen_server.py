"""An MCP Apps fixture server for reopening (plan MA18, MA19; AQ25, AQ26).

One tool, ``open_board(title, count)``, opens the view ``ui://boards/view``.
The view shows the arguments it was opened with and which call produced it, so
a replay is visible on screen.

Environment:

* ``APP_REOPEN_CALL_LOG``: a file that gets one JSON line per ``open_board``
  call, the evidence that a replay called the tool again with the saved
  arguments.
* ``APP_REOPEN_FAIL_FILE``: while this file exists, ``open_board`` fails, so a
  replay can be made to fail and then retried.

Run over stdio: ``python tests/fixtures/app_reopen_server.py``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError

VIEW_URI = "ui://boards/view"

server = FastMCP("boards")


def _log(entry: dict[str, object]) -> int:
    path = os.environ.get("APP_REOPEN_CALL_LOG")
    if not path:
        return 0
    log = Path(path)
    lines = log.read_text().splitlines() if log.exists() else []
    number = len(lines) + 1
    with log.open("a") as handle:
        handle.write(json.dumps({"call": number, **entry}) + "\n")
    return number


@server.tool(
    title="Open a board",
    app=AppConfig(resource_uri=VIEW_URI, visibility=["model", "app"]),
)
def open_board(title: str, count: int = 3) -> dict[str, object]:
    """Open a task board with ``count`` cards in an interactive view."""

    fail = os.environ.get("APP_REOPEN_FAIL_FILE")
    if fail and Path(fail).exists():
        _log({"title": title, "count": count, "failed": True})
        raise ToolError("The board service is unavailable right now.")
    opened_at = time.strftime("%H:%M:%S")
    number = _log({"title": title, "count": count, "opened_at": opened_at})
    cards = [f"{title} card {index + 1}" for index in range(max(0, min(count, 12)))]
    return {"title": title, "count": count, "cards": cards, "call": number, "opened_at": opened_at}


VIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="color-scheme" content="light dark">
  <title>Board</title>
  <style>
    :root { font: 14px/1.5 system-ui, sans-serif; }
    body { margin: 0; color: CanvasText; background: Canvas; }
    main { display: grid; gap: 10px; padding: 20px; }
    h1 { font-size: 18px; margin: 0; }
    ul { margin: 0; padding-left: 20px; }
    p { margin: 0; color: GrayText; }
  </style>
</head>
<body>
  <main>
    <h1 id="title">Waiting for the board</h1>
    <p id="args"></p>
    <ul id="cards"></ul>
    <p id="call" data-call=""></p>
  </main>
  <script>
  (() => {
    const send = (message) => window.parent.postMessage(message, '*');
    const size = () => requestAnimationFrame(() => send({jsonrpc: '2.0',
      method: 'ui/notifications/size-changed',
      params: {height: Math.ceil(document.querySelector('main').getBoundingClientRect().bottom)}}));
    window.addEventListener('message', (event) => {
      const m = event.data;
      if (!m || m.jsonrpc !== '2.0') return;
      if (m.id === 'boards-init' && m.result) {
        send({jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {}});
        size();
      } else if (m.method === 'ui/notifications/tool-input') {
        const a = m.params?.arguments || {};
        document.querySelector('#args').textContent =
          `Opened with title "${a.title}" and ${a.count ?? 3} cards`;
        size();
      } else if (m.method === 'ui/notifications/tool-result') {
        const r = m.params?.structuredContent || {};
        document.querySelector('#title').textContent = r.title || 'Board';
        const list = document.querySelector('#cards');
        list.replaceChildren(...(r.cards || []).map((text) => {
          const li = document.createElement('li'); li.textContent = text; return li;
        }));
        const call = document.querySelector('#call');
        call.dataset.call = String(r.call || '');
        call.textContent = `Produced by call ${r.call} at ${r.opened_at}`;
        size();
      }
    });
    send({jsonrpc: '2.0', id: 'boards-init', method: 'ui/initialize', params: {
      protocolVersion: '2026-01-26', appInfo: {name: 'boards', version: '1.0.0'},
      appCapabilities: {availableDisplayModes: ['inline', 'fullscreen']}}});
  })();
  </script>
</body>
</html>"""


@server.resource(VIEW_URI, app=AppConfig())
def board_view() -> str:
    """The board view."""

    return VIEW_HTML


if __name__ == "__main__":
    server.run(show_banner=False)
