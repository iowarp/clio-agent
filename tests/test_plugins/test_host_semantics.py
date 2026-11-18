"""Host semantics a plugin declares for its own MCP servers (HS1 to HS5)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id
from clio_agent.plugins import account_grants, host_semantics
from clio_agent.plugins.loader import PluginLoadError, load_plugin
from clio_agent.plugins.servers import server_declarations
from tests.test_plugins.helpers import plugin

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
GLOBUS_SCOPE = "https://auth.globus.org/scopes/00000000-0000-0000-0000-000000000000/all"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = tmp_path / "store"
    monkeypatch.setattr("clio_agent.plugins.service.plugin_store_root", lambda: store)
    monkeypatch.setattr(host_semantics, "_BY_PLUGIN", {})


def _fixture(tmp_path: Path, name: str) -> Path:
    copy = tmp_path / "plugins" / name
    shutil.copytree(FIXTURES / name, copy)
    return copy


def _stdio() -> dict[str, Any]:
    return {"type": "stdio", "command": "python3", "args": ["server.py"]}


def test_a_plugin_declares_its_own_server_without_a_connector(tmp_path: Path) -> None:
    record = load_plugin(_fixture(tmp_path, "own_server"), trusted=True)

    assert list(record.mcp_servers) == ["echo"]
    assert record.mcp_servers["echo"]["command"]
    assert not [d for d in record.diagnostics if d.severity == "error"]


def test_the_fixture_plugin_normalizes_every_host_semantic(tmp_path: Path) -> None:
    record = load_plugin(_fixture(tmp_path, "host_semantics"), trusted=True)

    assert record.host["tabs"] == [
        {
            "id": "panel",
            "title": "Fixture panel",
            "icon": "layout",
            "server": "hostfx",
            "resource": "a2ui://host-fixture/panel",
        }
    ]
    assert record.host["accounts"] == [{"provider": "globus", "scopes": [GLOBUS_SCOPE]}]
    assert record.host["elicitation"] == {"loopback": True, "origins": ["https://example.org"]}
    assert host_semantics.catalog_ids(record) == [basic_catalog_id()]


def test_server_host_slices_carry_full_catalog_ids_and_only_that_servers_tabs(
    tmp_path: Path,
) -> None:
    root = plugin(
        tmp_path,
        "two-servers",
        ext={
            "catalogs": ["basic"],
            "tabs": [
                {"id": "one", "title": "One", "server": "a", "resource": "a2ui://x/one"},
                {"id": "two", "title": "Two", "server": "b", "resource": "a2ui://x/two"},
            ],
        },
        servers={"a": _stdio(), "b": _stdio()},
    )
    record = load_plugin(root, trusted=True)

    a = host_semantics.server_host(record, "a")
    b = host_semantics.server_host(record, "b")

    assert a["catalogs"] == [basic_catalog_id()]
    assert [tab["id"] for tab in a["tabs"]] == ["one"]
    assert [tab["id"] for tab in b["tabs"]] == ["two"]
    assert host_semantics.server_host(load_plugin(_plain(tmp_path), trusted=True), "s") == {}


def _plain(tmp_path: Path) -> Path:
    return plugin(tmp_path, "plain", ext={}, servers={"s": _stdio()})


@pytest.mark.parametrize(
    ("ext", "servers", "fragment"),
    [
        (
            {"tabs": [{"id": "t", "title": "T", "server": "nope", "resource": "a2ui://x/t"}]},
            {"s": _stdio()},
            "unknown server",
        ),
        (
            {"tabs": [{"id": "t", "title": "T", "resource": "a2ui://x/t"}]},
            {"a": _stdio(), "b": _stdio()},
            "must name its server",
        ),
        (
            {"tabs": [{"id": "t", "title": "T", "resource": "https://x/t"}]},
            {"s": _stdio()},
            "a2ui://",
        ),
        (
            {"tabs": [{"id": "Bad Id", "title": "T", "resource": "a2ui://x/t"}]},
            {"s": _stdio()},
            ".id must be",
        ),
        ({"accounts": [{"provider": "nowhere"}]}, {"s": _stdio()}, ".provider must be"),
        (
            {"accounts": [{"provider": "globus"}, {"provider": "globus"}]},
            {"s": _stdio()},
            "once each",
        ),
        ({"elicitation": {"origins": ["http://x.org"]}}, {"s": _stdio()}, "https://"),
        ({"elicitation": {"loopback": "yes"}}, {"s": _stdio()}, "true or false"),
        ({"elicitation": {"other": True}}, {"s": _stdio()}, "only loopback and origins"),
    ],
)
def test_bad_host_declarations_fail_to_load(
    tmp_path: Path, ext: dict[str, Any], servers: dict[str, Any], fragment: str
) -> None:
    root = plugin(tmp_path, "bad", ext=ext, servers=servers)

    with pytest.raises(PluginLoadError, match=fragment):
        load_plugin(root, trusted=True)


def _rows(record: Any) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "plugin": record.id,
            "root": record.root,
            "spec": spec,
            "host": host_semantics.server_host(record, name),
        }
        for name, spec in record.mcp_servers.items()
    }


def test_server_declarations_name_the_grant_file_never_the_token(tmp_path: Path) -> None:
    record = load_plugin(_fixture(tmp_path, "host_semantics"), trusted=True)

    declared = server_declarations(_rows(record))["host-fixture"]["hostfx"]

    path = str(account_grants.grant_path("host-fixture", "globus"))
    assert declared["env"]["CLIO_ACCOUNT_GLOBUS_FILE"] == path
    assert declared["args"][-1] == path
    assert not any("${" in arg for arg in declared["args"])
    assert "access_token" not in repr(declared)


def test_remembered_slices_give_declared_accounts_and_catalogs(tmp_path: Path) -> None:
    record = load_plugin(_fixture(tmp_path, "host_semantics"), trusted=True)
    host_semantics.remember(_rows(record))

    assert host_semantics.declared_accounts() == {"globus": {"host-fixture": [GLOBUS_SCOPE]}}
    assert host_semantics.declared_account_scopes("globus") == [GLOBUS_SCOPE]
    assert host_semantics.declared_account_scopes("google_drive") == []
    assert host_semantics.plugin_host("host-fixture", "hostfx")["catalogs"] == [basic_catalog_id()]

    class Spec:
        source = "plugin:host-fixture"
        name = "hostfx"

    assert host_semantics.spec_a2ui_catalogs(Spec()) == (basic_catalog_id(),)
    assert host_semantics.spec_plugin("connector:x") == ""
    assert host_semantics.account_variable("google_drive") == "CLIO_ACCOUNT_GOOGLE_DRIVE_FILE"


def test_a_grant_file_holds_only_documented_fields_and_is_private() -> None:
    path = account_grants.write_grant(
        "p",
        "globus",
        [
            {
                "resource_server": "transfer.api.globus.org",
                "access_token": "tok",
                "refresh_token": "never",
                "expires_at": "2030-01-01T00:00:00Z",
                "scopes": ["s"],
            },
            {"resource_server": "empty", "access_token": ""},
        ],
    )

    assert path is not None
    document = account_grants.read_grant("p", "globus")
    assert document is not None
    assert document["version"] == 1
    assert document["provider"] == "globus"
    assert document["tokens"] == [
        {
            "resource_server": "transfer.api.globus.org",
            "access_token": "tok",
            "token_type": "Bearer",
            "expires_at": "2030-01-01T00:00:00Z",
            "scopes": ["s"],
        }
    ]
    assert "never" not in path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".grant-")]


def test_a_grant_without_tokens_removes_the_file() -> None:
    account_grants.write_grant("p", "globus", [{"access_token": "tok"}])

    assert account_grants.write_grant("p", "globus", [{"access_token": ""}]) is None
    assert account_grants.read_grant("p", "globus") is None
    assert account_grants.remove_grant("p", "globus") is False
