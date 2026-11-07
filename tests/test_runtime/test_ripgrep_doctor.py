"""The doctor says which ripgrep file search runs, and says so when there is none."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime import ripgrep
from clio_agent.runtime.doctor_rows import optional_rows
from clio_agent.runtime.status import IntegrationState


def test_a_missing_ripgrep_is_a_degraded_row_naming_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: report READY when nothing is found -> red."""
    monkeypatch.setattr(ripgrep, "installed_ripgrep", lambda **_: None)
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _name: None)
    row = ripgrep.probe_ripgrep()
    assert row.state is IntegrationState.DEGRADED
    assert ripgrep.RIPGREP_MISSING in row.summary
    assert row.required is False


def test_a_host_ripgrep_is_ready_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ripgrep, "installed_ripgrep", lambda **_: None)
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _name: "/usr/bin/rg")
    row = ripgrep.probe_ripgrep()
    assert row.state is IntegrationState.READY
    assert row.details["executable"] == str(Path("/usr/bin/rg"))
    assert row.details["source"] == "host PATH"


def test_the_doctor_lists_the_ripgrep_row() -> None:
    assert "ripgrep" in [row.name for row in optional_rows()]
