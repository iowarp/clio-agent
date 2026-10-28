"""``clio-agent doctor`` runs end to end: every module it imports exists."""

from __future__ import annotations

import json
from typing import Any

import pytest

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

    assert doctor.run_doctor(json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == {"integrations": []}
