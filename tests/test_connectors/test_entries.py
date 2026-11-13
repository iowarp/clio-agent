"""Connector entries: schema, placement rules, folders (CN1, CN2, CN11)."""

from __future__ import annotations

from pathlib import Path

import yaml

from clio_agent.connectors import entries


def _write(folder: Path, rows: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "entries.yaml").write_text(yaml.safe_dump(rows), encoding="utf-8")
    return folder


def _entry(**overrides: object) -> dict:
    row = {
        "id": "demo",
        "name": "Demo",
        "description": "A demo",
        "server": {"url": "${need.address}"},
        "needs": [{"key": "address", "kind": "address", "label": "Address"}],
    }
    row.update(overrides)
    return row


def test_the_fixture_catalog_loads_three_connectors(fixture_catalog: Path) -> None:
    catalog = entries.load_catalog()
    assert {"fixture-plain", "fixture-signin", "fixture-secret"} <= set(catalog.entries)
    assert not catalog.diagnostics
    secret = catalog.entries["fixture-secret"]
    assert secret.need("token").kind == "secret"
    assert secret.need("where").agent_settable and not secret.need("where").required
    assert secret.logo_path is not None and secret.logo_path.is_file()
    assert catalog.entries["fixture-signin"].logo == ""


def test_placement_rules_refuse_what_the_schema_cannot(tmp_path: Path) -> None:
    secret_in_args = _entry(
        server={"command": "srv", "args": ["--token", "${need.token}"]},
        needs=[{"key": "token", "kind": "secret", "label": "Token"}],
    )
    unknown = _entry(server={"url": "${need.nowhere}"})
    sign_in_on_command = _entry(
        server={"command": "srv"}, needs=[{"key": "a", "kind": "sign-in", "label": "A"}]
    )
    settable_secret = _entry(
        server={"url": "https://x.test/mcp", "headers": {"X-Key": "${need.k}"}},
        needs=[{"key": "k", "kind": "secret", "label": "K", "agent_settable": True}],
    )
    assert "only be used in headers or env" in " ".join(entries.placement_errors(secret_in_args))
    assert "names no need" in " ".join(entries.placement_errors(unknown))
    assert "needs a URL server" in " ".join(entries.placement_errors(sign_in_on_command))
    assert "agent_settable" in " ".join(entries.placement_errors(settable_secret))
    ok = _entry(
        server={"url": "https://x.test/mcp", "headers": {"X-Key": "${need.k}"}},
        needs=[{"key": "k", "kind": "secret", "label": "K"}],
    )
    assert entries.placement_errors(ok) == []


def test_an_invalid_entry_is_reported_and_the_others_load(tmp_path: Path) -> None:
    folder = _write(
        tmp_path / "a",
        [
            _entry(),
            _entry(id="Bad Id"),
            _entry(id="logo-missing", logo="nowhere.svg"),
        ],
    )
    later = _write(tmp_path / "b", [_entry(name="Shadow")])
    catalog = entries.load_catalog([folder, later])
    assert list(catalog.entries) == ["demo"]
    assert catalog.entries["demo"].name == "Demo"
    codes = sorted((row.code, row.entry) for row in catalog.diagnostics)
    assert codes == [
        (entries.ENTRY_DUPLICATE, "demo"),
        (entries.ENTRY_INVALID, "Bad Id"),
        (entries.ENTRY_INVALID, "logo-missing"),
    ]


def test_shipped_folder_has_the_schema() -> None:
    assert (entries.shipped_dir() / entries.SCHEMA_FILE).is_file()
