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


class FakeSessions:
    """A session store with metadata only: ``get`` and ``update(metadata_patch=...)``."""

    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    def get(self, sid: str) -> object:
        from types import SimpleNamespace

        return self.rows.setdefault(sid, SimpleNamespace(id=sid, metadata={}, workspace_id=""))

    def update(self, sid: str, *, metadata_patch: dict | None = None, **_: object) -> object:
        row = self.get(sid)
        row.metadata = {**row.metadata, **(metadata_patch or {})}  # type: ignore[attr-defined]
        return row
