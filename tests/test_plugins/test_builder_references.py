"""The builder's references: every worked example passes ``clio-agent plugin check`` (B8).

Each ``skills/capture/references/<part>.md`` ends with a ``## Worked example`` whose
fenced blocks are tagged ``<language> <path>``: together they are one plugin, written to
a folder and checked as an install would check it. The builder writes plugins from
these examples, so an example that fails the check teaches the builder to fail.
"""

from __future__ import annotations

import http.server
import json
import re
import sys
import threading
from pathlib import Path

import pytest

from clio_agent.plugins.check import check
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore, builtin_records
from tests.test_plugins.helpers import NATIVE

BUILDER = Path(__file__).parents[2] / "src/clio_agent/plugins/builtin/builder"
REFERENCES = BUILDER / "skills/capture/references"
PART_TYPES = {
    "skill",
    "fragment",
    "hook",
    "tool",
    "flow",
    "subagent",
    "command",
    "connection",
    "agent",
    "view",
    "app",
}
_BLOCK = re.compile(r"^```(\w+) (\S+)\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def _example(reference: Path) -> dict[str, str]:
    text = reference.read_text(encoding="utf-8")
    _, _, example = text.partition("## Worked example")
    return {path: body for _lang, path, body in _BLOCK.findall(example)}


def test_there_is_one_reference_per_part_type() -> None:
    assert {p.stem for p in REFERENCES.glob("*.md")} == PART_TYPES


@pytest.mark.parametrize("part", sorted(PART_TYPES))
def test_the_worked_example_passes_plugin_check(part: str, tmp_path: Path) -> None:
    files = _example(REFERENCES / f"{part}.md")
    assert "plugin.json" in files, f"{part}.md: the worked example needs a plugin.json block"
    folder = tmp_path / "example"
    for path, body in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    native = NATIVE | {t.name for r in builtin_records() for t in r.tools if t.kind == "native"}
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=native)
    report = check(folder, service=service, trusted=True)
    errors = [d for d in report.diagnostics if d.severity == "error"]
    assert report.ok and not errors, [str(d) for d in errors]
    warnings = [d.code for d in report.diagnostics if d.severity == "warning"]
    assert "fragment_budget" not in warnings and "skill_invalid" not in warnings, warnings


@pytest.mark.parametrize("plugin", ["guide", "builder"])
def test_the_built_in_plugin_passes_plugin_check(plugin: str, tmp_path: Path) -> None:
    native = NATIVE | {t.name for r in builtin_records() for t in r.tools if t.kind == "native"}
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=native)
    report = check(BUILDER.parent / plugin, service=service, trusted=True)
    assert report.ok, [str(d) for d in report.diagnostics]


def test_the_view_example_is_a_valid_chart_over_the_runs_file() -> None:
    """The A2UI example's component passes the clio-workspace catalog's validator."""
    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415
    from clio_agent.gact.a2ui_catalogs.validation import validate_components  # noqa: PLC0415

    skill = _example(REFERENCES / "view.md")["skills/training-curves/SKILL.md"]
    block = [line[8:] for line in skill.splitlines() if line.startswith("        ")]
    component = json.loads("\n".join(block))
    assert component["component"] == "clio.chart.v1"
    assert component["dataUri"].endswith("train_log.csv@latest")
    (catalog,) = [entry for entry in load_builtin_catalogs() if entry.name == "clio-workspace"]
    validate_components(catalog, [component])


async def test_the_app_example_shows_the_programs_data_and_asks_it_for_more(
    tmp_path: Path,
) -> None:
    """The MCP App example: its opening tool names the page, the page is served as an
    app, the fleet and a float's profile come from the program's service (a stand-in
    here), and the page's own tool is for the page only."""
    from fastmcp import Client  # noqa: PLC0415
    from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

    floats = [{"id": "4903", "lat": 58.1, "lon": -52.4, "active": True}]
    profile = {"time": "2026-11-16T04:00:00Z", "levels": [{"depth_m": 0, "temp_c": 4.2}]}
    seen: list[str] = []

    class Program(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
            seen.append(self.path)
            body = (
                floats
                if self.path.startswith("/api/floats?") or self.path == "/api/floats"
                else profile
            )
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return

    program = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Program)
    threading.Thread(target=program.serve_forever, daemon=True).start()
    server = tmp_path / "server.py"
    server.write_text(_example(REFERENCES / "app.md")["server.py"], encoding="utf-8")
    api = f"http://127.0.0.1:{program.server_address[1]}/api"
    transport = StdioTransport(command=sys.executable, args=[str(server)], env={"FLEET_API": api})
    try:
        async with Client(transport) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            uri = tools["show_fleet"].meta["ui"]["resourceUri"]
            assert tools["float_profile"].meta["ui"]["visibility"] == ["app"]
            (page,) = await client.read_resource(uri)
            assert page.mimeType == "text/html;profile=mcp-app"
            assert "tools/call" in page.text and "float_profile" in page.text
            fleet = await client.call_tool("show_fleet", {"region": "labrador"})
            assert fleet.structured_content == {"floats": floats}
            picked = await client.call_tool("float_profile", {"float_id": "4903"})
            assert picked.structured_content == profile
    finally:
        program.shutdown()
    assert seen == ["/api/floats?region=labrador", "/api/floats/4903/profile"]
