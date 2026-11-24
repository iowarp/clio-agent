"""A pinned session's plugin servers start from the installed copy (one copy per data dir)."""

from __future__ import annotations

from clio_agent.plugins.installed_servers import follow_installed
from clio_agent.plugins.records import PluginRecord
from clio_agent.plugins.server_rows import server_row

SPEC = {"command": "uv", "args": ["run", "${PLUGIN_ROOT}/server"]}


def _record(plugin: str, root: str, servers: dict | None = None) -> PluginRecord:
    return PluginRecord(
        id=plugin,
        version="1",
        description="",
        kind="plugin",
        format="agent-plugins",
        root=root,
        marketplace="market",
        mcp_servers=servers if servers is not None else {plugin: dict(SPEC)},
    )


def _rows(*records: PluginRecord) -> dict:
    return {
        name: server_row(record, name, spec)
        for record in records
        for name, spec in record.mcp_servers.items()
    }


def test_a_pin_on_the_installed_copy_is_unchanged() -> None:
    relay = _record("relay", "/snap/new")
    rows = _rows(relay)
    assert follow_installed(rows, [relay], lambda _record: True) == rows


def test_an_older_copy_starts_from_the_installed_one() -> None:
    old, new = _record("relay", "/snap/old"), _record("relay", "/snap/new")
    other = _record("files", "/snap/files")
    followed = follow_installed(_rows(old, other), [new, other], lambda _record: True)
    assert followed["relay"]["root"] == "/snap/new"
    assert followed["files"]["root"] == "/snap/files"


def test_the_installed_copy_brings_its_own_servers() -> None:
    old = _record("relay", "/snap/old", {"relay": dict(SPEC), "relay-gone": dict(SPEC)})
    new = _record("relay", "/snap/new", {"relay": dict(SPEC), "relay-pages": dict(SPEC)})
    followed = follow_installed(_rows(old), [new], lambda _record: True)
    assert sorted(followed) == ["relay", "relay-pages"]
    assert {row["root"] for row in followed.values()} == {"/snap/new"}


def test_a_moved_copy_not_trusted_starts_no_server() -> None:
    old, new = _record("relay", "/snap/old"), _record("relay", "/snap/new")
    other = _record("files", "/snap/files")
    followed = follow_installed(
        _rows(old, other), [new, other], lambda record: record.id != "relay"
    )
    assert sorted(followed) == ["files"]


def test_an_uninstalled_or_ambiguous_plugin_keeps_its_pin() -> None:
    old = _record("relay", "/snap/old")
    rows = _rows(old)
    assert follow_installed(rows, [], lambda _record: True) == rows
    twice = [_record("relay", "/snap/a"), _record("relay", "/snap/b")]
    assert follow_installed(rows, twice, lambda _record: True) == rows
