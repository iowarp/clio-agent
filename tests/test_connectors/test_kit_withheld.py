"""A clio-kit server CLIO reaches another way is not offered as a connector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.connectors import kit_entries

CATALOGS = Path(__file__).resolve().parents[2] / "catalogs" / "connectors"


def test_ndp_is_withheld_with_its_reason_and_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogue = {
        "packages": {},
        "servers": {
            "ndp": {"command": "clio-kit", "args": ["mcp-server", "ndp"]},
            "pandas": {"command": "clio-kit", "args": ["mcp-server", "pandas"]},
        },
    }
    monkeypatch.setattr(kit_entries, "components", lambda: catalogue)
    traced: list[tuple[Any, ...]] = []
    monkeypatch.setattr("clio_agent.runtime.trace.event", lambda *a: traced.append(a))
    ids = [row["id"] for row in kit_entries.derived_raw(CATALOGS)]
    assert ids == ["pandas"]
    (event,) = traced
    assert event[2:4] == (kit_entries.KIT_SERVER_WITHHELD, "ndp")
    assert "NDP source" in event[4]
