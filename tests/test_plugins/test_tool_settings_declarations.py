"""Plugins declare each tool's I/O (``callers``) and default permission (TD52, TD53)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins.check import check
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.manifest import ManifestError, clio_extension
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore
from clio_agent.sdk import native_tool, tool
from tests.test_plugins.helpers import plugin, write

_SERVER = {"type": "stdio", "command": "python", "args": ["server.py"]}
_TOOLS = (
    "from clio_agent.sdk import native_tool, tool\n"
    "view_pdf = native_tool('view_pdf', callers='code', permission='ask')\n"
    "view_image = native_tool('view_image')\n"
    "@tool(read_only=True, callers='direct', permission='deny')\n"
    "def lookup(key: str) -> str:\n"
    "    return key\n"
)


def test_sdk_declarations_reach_the_record(tmp_path: Path) -> None:
    root = plugin(tmp_path, "viewers", files={"ai.iowarp.clio/tools/t.py": _TOOLS})
    record = load_plugin(root, trusted=True)
    by_name = {t.name: (t.callers, t.permission) for t in record.tools}
    assert by_name == {
        "view_pdf": ("code", "ask"),
        "view_image": (None, None),
        "lookup": ("direct", "deny"),
    }


@pytest.mark.parametrize("kwargs", [{"callers": "script"}, {"permission": "block"}])
def test_sdk_rejects_unknown_values(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="must be one of"):
        native_tool("view_pdf", **kwargs)
    with pytest.raises(ValueError, match="must be one of"):
        tool(lambda: None, name="x", **kwargs)


def test_server_and_tool_entries_are_read_from_the_extension(tmp_path: Path) -> None:
    ext = {
        "servers": {
            "home": {
                "callers": "code",
                "permission": "ask",
                "tools": {"light_list": {"callers": "both", "permission": "allow"}},
            }
        }
    }
    root = plugin(tmp_path, "home", ext=ext, servers={"home": _SERVER})
    assert load_plugin(root, trusted=True).disclosure == ext


@pytest.mark.parametrize(
    "ext",
    [
        {"servers": {"home": {"callers": "script"}}},
        {"servers": {"home": {"permission": "block"}}},
        {"servers": {"home": {"tools": ["light_list"]}}},
        {"servers": {"home": {"tools": {"light_list": {"callers": "nobody"}}}}},
        {"servers": {"home": {"tools": {"light_list": {"permission": "never"}}}}},
        {"servers": {"home": {"tools": {"light_list": {"defer": True}}}}},
    ],
)
def test_invalid_settings_are_refused(ext: dict) -> None:
    with pytest.raises(ManifestError):
        clio_extension({"extensions": {"ai.iowarp.clio": ext}}, "p")


def test_plugin_check_rejects_unknown_values(tmp_path: Path) -> None:
    root = tmp_path / "market"
    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": "m"}))
    plugin(
        root / "plugins",
        "bad-server",
        servers={"home": _SERVER},
        ext={"servers": {"home": {"callers": "script"}}},
    )
    plugin(
        root / "plugins",
        "bad-tool",
        files={
            "ai.iowarp.clio/tools/t.py": (
                "from clio_agent.sdk import native_tool\n"
                "view_pdf = native_tool('view_pdf', permission='block')\n"
            )
        },
    )
    plugin(root / "plugins", "fine")
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=frozenset({"view_pdf"}))
    report = check(root, service=service)
    assert not report.ok
    text = report.render()
    assert "callers must be one of direct, code, both, not 'script'" in text
    assert "permission must be one of allow, ask, deny, not 'block'" in text
