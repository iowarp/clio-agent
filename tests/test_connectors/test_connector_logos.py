"""Connector logos (apps ledger, "Connector logos (LG)").

A derived clio-kit connector takes its logo from ``derive.yaml``'s ``logos`` map
(LG-D2), validated and served like a hand-written entry's ``logo``; an entry may give a
dark-theme variant, ``logo_dark``, which rows name as ``logo_dark_url`` and
``GET /v1/connectors/{id}/logo?theme=dark`` serves (LG-D1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.connectors import entries, kit_entries

SHIPPED = Path(__file__).resolve().parents[2] / "catalogs" / "connectors"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24"/></svg>'

CATALOGUE = {
    "servers": {
        name: {"artifact": f"server/{name}"} for name in ("alpha", "beta", "gamma", "delta")
    },
    "packages": {},
}


@pytest.fixture
def kit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A shipped folder whose derive.yaml gives logos: one plain, one with a dark variant,
    one naming a missing file; and one hand-written entry with a dark variant."""

    shipped = tmp_path / "shipped"
    derive = shipped / "clio-kit"
    derive.mkdir(parents=True)
    for name in ("alpha.svg", "beta.svg", "beta-dark.svg"):
        (derive / name).write_text(SVG.replace("<rect", f"<!-- {name} --><rect"))
    (derive / "derive.yaml").write_text(
        "logos:\n  alpha: alpha.svg\n  beta: {light: beta.svg, dark: beta-dark.svg}\n"
        "  gamma: missing.svg\n"
    )
    (shipped / "connector.schema.json").write_text((SHIPPED / "connector.schema.json").read_text())
    (shipped / "own.svg").write_text(SVG)
    (shipped / "own-dark.svg").write_text(SVG.replace("<rect", "<!-- dark --><rect"))
    (shipped / "written.yaml").write_text(
        "- id: written\n  name: Written\n  description: By hand\n  logo: own.svg\n"
        "  logo_dark: own-dark.svg\n  server:\n    command: x\n"
    )
    monkeypatch.setattr(kit_entries, "components", lambda: CATALOGUE)
    monkeypatch.setattr(entries, "shipped_dir", lambda: shipped)
    monkeypatch.delenv("CLIO_CONNECTOR_CATALOG_DIRS", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entries._CACHE.clear()
    entries._validator.cache_clear()
    yield shipped
    entries._CACHE.clear()
    entries._validator.cache_clear()


def test_derived_entries_take_their_logos_and_dark_variants(kit: Path) -> None:
    catalog = entries.load_catalog()
    alpha, beta, delta = (catalog.entries[cid] for cid in ("alpha", "beta", "delta"))
    assert (alpha.logo, alpha.logo_dark) == ("alpha.svg", "")
    assert alpha.logo_path == kit / "clio-kit" / "alpha.svg"
    assert alpha.logo_urls() == {"logo_url": "/v1/connectors/alpha/logo", "logo_dark_url": None}
    assert (beta.logo, beta.logo_dark) == ("beta.svg", "beta-dark.svg")
    assert beta.theme_logo_path("dark") == kit / "clio-kit" / "beta-dark.svg"
    assert beta.logo_urls()["logo_dark_url"] == "/v1/connectors/beta/logo?theme=dark"
    # No logo listed: a monogram, as before.
    assert delta.logo == "" and delta.logo_urls() == {"logo_url": None, "logo_dark_url": None}
    # A logo naming no file is a typed diagnostic, like a hand-written entry's.
    assert "gamma" not in catalog.entries
    (bad,) = catalog.diagnostics
    assert (bad.code, bad.entry) == (entries.ENTRY_INVALID, "gamma")
    assert "logo 'missing.svg' is not beside the entry" in bad.message


def test_a_dark_variant_needs_a_logo_and_an_svg_beside_it(tmp_path: Path) -> None:
    (tmp_path / "a.svg").write_text(SVG)
    base = {"id": "x", "name": "X", "description": "d", "server": {"command": "x"}}
    assert (
        entries.validate_entry({**base, "logo": "a.svg", "logo_dark": "a.svg"}, folder=tmp_path)
        == []
    )
    assert any(
        "logo" in error
        for error in entries.validate_entry({**base, "logo_dark": "a.svg"}, folder=tmp_path)
    )
    assert entries.validate_entry(
        {**base, "logo": "a.svg", "logo_dark": "b.svg"}, folder=tmp_path
    ) == ["logo_dark 'b.svg' is not beside the entry"]
    assert entries.validate_entry({**base, "logo": "a.svg", "logo_dark": "a.png"}, folder=tmp_path)


def test_the_logo_route_serves_each_theme(kit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import connectors_service as service
    from clio_agent.gact.routes.connectors import register_connector_routes

    monkeypatch.setattr(service, "entry", lambda cid: entries.load_catalog().entries[cid])
    app = FastAPI()
    register_connector_routes(app)
    client = TestClient(app)
    light = client.get("/v1/connectors/beta/logo")
    dark = client.get("/v1/connectors/beta/logo?theme=dark")
    assert light.status_code == dark.status_code == 200
    assert "beta.svg" in light.text and "beta-dark.svg" in dark.text
    assert dark.headers["content-type"].startswith("image/svg+xml")
    assert dark.headers["content-security-policy"].startswith("default-src 'none'")
    assert "own-dark.svg" not in client.get("/v1/connectors/written/logo").text
    assert "dark" in client.get("/v1/connectors/written/logo?theme=dark").text
    # One logo for both themes: the dark one is not there, and rows name none.
    assert client.get("/v1/connectors/alpha/logo?theme=dark").status_code == 404
    assert client.get("/v1/connectors/alpha/logo").status_code == 200
    assert client.get("/v1/connectors/delta/logo").status_code == 404


def test_every_shipped_logo_loads_and_every_clio_kit_server_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped derive.yaml names a logo beside it for every server it names."""

    derive = yaml.safe_load((SHIPPED / "clio-kit" / "derive.yaml").read_text())
    logos = derive["logos"]
    servers = {*derive["names"], *logos} - {"paraview", "geo"}
    assert servers <= set(logos), "a clio-kit server without a logo shows a monogram"
    catalogue = {"servers": {name: {} for name in servers}, "packages": {}}
    monkeypatch.setattr(kit_entries, "components", lambda: catalogue)
    catalog = entries.Catalog()
    entries.load_folder(SHIPPED, catalog)
    entries.add_derived(SHIPPED, catalog)
    assert catalog.diagnostics == []
    for entry in catalog.entries.values():
        for theme in ("", "dark"):
            path = entry.theme_logo_path(theme)
            if path is not None:
                assert path.read_text().lstrip().startswith(("<svg", "<?xml")), path
                assert "<script" not in path.read_text()
    assert catalog.entries["ndp"].logo_dark == "ndp-dark.svg"
    assert catalog.entries["pdf"].logo_dark == "pdf-dark.svg"
    # Every connector shipped here has a logo; none falls back to a monogram.
    assert [cid for cid, entry in catalog.entries.items() if not entry.logo] == []
    assert json.loads((SHIPPED / "connector.schema.json").read_text())["$defs"]["connector"][
        "dependentRequired"
    ] == {"logo_dark": ["logo"]}
