"""The MCP Apps probe reports a server's views as CLIO sees them (apps ledger, grounds)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mcp_app_probe.py"
_spec = importlib.util.spec_from_file_location("mcp_app_probe", _SCRIPT)
assert _spec and _spec.loader
probe_module: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_module)

VIEW = """<!doctype html><script>
const app = new App({name: 'v', version: '1'}, {availableDisplayModes: ['inline', 'fullscreen']});
app.ontoolresult = (r) => draw(r);
await app.connect();
await app.requestDisplayMode({mode: 'fullscreen'});
app.openLink({url: 'https://example.org'});
</script>"""


def _server() -> FastMCP:
    server = FastMCP("fixture-views")

    @server.tool(meta={"ui": {"resourceUri": "ui://fixture/view"}})
    def show(item: str, depth: int = 1) -> str:
        """Open the view."""
        return item * depth

    @server.tool
    def plain(x: int) -> int:
        """No view."""
        return x

    @server.resource(
        "ui://fixture/view",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"csp": {"connectDomains": ["https://api.example.org"]}}},
    )
    def view() -> str:
        return VIEW

    return server


def test_view_features_reads_methods_and_sdk_calls() -> None:
    features = probe_module.view_features(VIEW + "\nwindow.openai.toolOutput;")
    assert features["methods"]["ui/open-link"]["sdk_calls"] == {"openLink": 1}
    assert "ui/request-display-mode" in features["methods"]
    assert features["display_mode_strings"] == ["fullscreen", "inline"]
    assert features["capability_hints"] == ["availableDisplayModes"]
    assert features["other_host_api"] == ["toolOutput"]


def test_ui_meta_accepts_the_flat_key() -> None:
    assert probe_module._ui_meta({"ui/resourceUri": "ui://a/b"}) == {
        "resourceUri": "ui://a/b",
        "flat_key": True,
    }
    assert probe_module._ui_meta(None) == {}


@pytest.mark.asyncio
async def test_probe_reports_tools_and_view_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server()
    monkeypatch.setattr(probe_module, "transport_from_spec", lambda spec: spec)
    monkeypatch.setattr(probe_module, "make_mcp_client", lambda target, server_id: Client(server))

    report = await probe_module.probe({"transport": "memory"}, "fixture")

    assert report["server_info"]["name"] == "fixture-views"
    tools = {row["name"]: row for row in report["tools"]}
    assert tools["show"]["ui"] == {"resourceUri": "ui://fixture/view"}
    assert tools["show"]["params"] == ["depth", "item"]
    assert tools["plain"]["ui"] is None
    [resource] = report["ui_resources"]
    assert resource["tools"] == ["show"]
    [content] = resource["contents"]
    assert content["mime_type"] == "text/html;profile=mcp-app"
    assert content["bytes"] == len(VIEW.encode())
    assert "ui/open-link" in content["features"]["methods"]
    assert "ui://fixture/view" in probe_module._summary(report)
