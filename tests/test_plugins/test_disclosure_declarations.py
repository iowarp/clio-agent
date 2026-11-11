"""Plugins declare which tools and servers are deferred, and what a server is for."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.plugins.check import _undescribed_servers
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.manifest import ManifestError, clio_extension
from tests.test_plugins.helpers import plugin

_SERVER = {"type": "stdio", "command": "python", "args": ["server.py"]}
_DEFERRED_TOOLS = (
    "from clio_agent.sdk import native_tool\n"
    "view_pdf = native_tool('view_pdf', defer=True)\n"
    "view_image = native_tool('view_image')\n"
)


def test_native_tool_defer_reaches_the_record(tmp_path: Path) -> None:
    root = plugin(tmp_path, "viewers", files={"ai.iowarp.clio/tools/t.py": _DEFERRED_TOOLS})
    record = load_plugin(root, trusted=True)
    by_name = {tool.name: tool.defer for tool in record.tools}
    assert by_name == {"view_pdf": True, "view_image": None}


def test_server_disclosure_is_read_from_the_extension(tmp_path: Path) -> None:
    ext = {"defer": False, "servers": {"home": {"defer": True, "description": "Smart home."}}}
    root = plugin(tmp_path, "home", ext=ext, servers={"home": _SERVER})
    record = load_plugin(root, trusted=True)
    assert record.disclosure == ext
    assert _undescribed_servers(record) == []


@pytest.mark.parametrize(
    "ext",
    [
        {"defer": "yes"},
        {"servers": ["home"]},
        {"servers": {"home": {"defer": True, "colour": "red"}}},
        {"servers": {"home": {"defer": 1}}},
        {"servers": {"home": {"description": 3}}},
    ],
)
def test_invalid_disclosure_is_refused(ext: dict) -> None:
    with pytest.raises(ManifestError):
        clio_extension({"extensions": {"ai.iowarp.clio": ext}}, "p")


def test_one_server_takes_the_plugin_description(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "home",
        servers={"home": _SERVER},
        manifest_extra={"description": "A smart home."},
    )
    assert _undescribed_servers(load_plugin(root, trusted=True)) == []


def test_undescribed_servers_warn(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "multi",
        servers={"home": _SERVER, "calendar": _SERVER},
        manifest_extra={"description": "Two servers."},
        ext={"servers": {"calendar": {"description": "Events."}}},
    )
    found = _undescribed_servers(load_plugin(root, trusted=True))
    assert [(d.severity, d.code) for d in found] == [("warning", "deferred_server_undescribed")]
    assert "'home'" in found[0].message
