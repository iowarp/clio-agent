"""Connector tests read the fixture catalog (tests/fixtures/connectors)."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def fixture_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Enable the fixture connectors and give the test its own cache folder."""

    monkeypatch.setenv("CLIO_CONNECTOR_CATALOG_DIRS", str(FIXTURES / "connectors"))
    monkeypatch.setenv("CLIO_CONNECTOR_FIXTURE_DIR", str(FIXTURES))
    monkeypatch.setenv("CLIO_CONNECTOR_FIXTURE_PYTHON", "python3")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return FIXTURES / "connectors"
