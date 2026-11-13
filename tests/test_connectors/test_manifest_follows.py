"""``follows`` in the ai.iowarp.clio extension (CN9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.manifest import ManifestError, clio_extension

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/connectors/marketplace/plugins"


def _manifest(ext: dict) -> dict:
    return {"name": "p", "extensions": {"ai.iowarp.clio": ext}}


def test_follows_needs_the_connectors_own_connection() -> None:
    ok = {"follows": "demo", "connections": {"demo": "demo"}}
    assert clio_extension(_manifest(ok), "p")["follows"] == "demo"
    for bad in (
        {"follows": "demo"},
        {"follows": "demo", "connections": {"other": "demo"}},
        {"follows": "Not Valid"},
    ):
        with pytest.raises(ManifestError, match="follows"):
            clio_extension(_manifest(bad), "p")


def test_the_fixture_plugin_loads_with_its_flag() -> None:
    record = load_plugin(_FIXTURE / "fixture-secret-skills", trusted=False)
    assert record.follows == "fixture-secret"
    assert record.mcp_servers == {
        "fixture-secret": {"type": "clio-connection", "connection": "fixture-secret"}
    }
    assert type(record).from_dict(record.to_dict()).follows == "fixture-secret"
