"""``clio-agent doctor`` runs end to end: every module it imports exists."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact import store_format_boot
from clio_agent.gact.routes import provider_probe_env
from clio_agent.runtime import status
from clio_agent.ui import doctor


class _Report:
    integrations: list[Any] = []

    def to_dict(self) -> dict[str, Any]:
        return {"integrations": []}


def test_doctor_json_runs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A removed probe module left behind as an import crashed every doctor run."""
    monkeypatch.setattr(status, "collect_runtime_status", lambda **_: _Report())
    monkeypatch.setattr(provider_probe_env, "runtime_provider_probe_env", lambda _: {})
    monkeypatch.setattr(store_format_boot, "doctor_rows", lambda: [])

    assert doctor.run_doctor(json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["integrations"] == []


def test_doctor_reports_data_an_older_clio_wrote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The standalone doctor carries the ``arc_store_format`` row (read from the store)."""
    row = status.IntegrationStatus(
        name="arc_store_format",
        state=status.IntegrationState.UNAVAILABLE,
        summary="Your CLIO data in /d was written by CLIO 0.9.5.1 or older.",
        config_source="clio-core store",
        next_action="Quit CLIO, then run `clio reset-store`.",
    )
    monkeypatch.setattr(status, "collect_runtime_status", lambda **_: _Report())
    monkeypatch.setattr(provider_probe_env, "runtime_provider_probe_env", lambda _: {})
    monkeypatch.setattr(store_format_boot, "doctor_rows", lambda: [row])

    assert doctor.run_doctor(json_output=True) == 0
    [shown] = json.loads(capsys.readouterr().out)["integrations"]
    assert shown["name"] == "arc_store_format" and "reset-store" in shown["next_action"]
