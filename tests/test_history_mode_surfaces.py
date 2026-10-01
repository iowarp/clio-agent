"""History mode is loud: health, doctor and the boot record all say which mode CLIO runs in.

A CLIO without clio-core is a working, degraded server -- never "service down" (503) and
never silent: the ``arc`` row is DEGRADED with the mode, its reason and the remedy, the
health response carries ``context_mode``, and the boot records a ``context.mode`` event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc import history_mode
from clio_agent.gact import server_boot
from clio_agent.gact.app import build_app
from clio_agent.runtime.status import IntegrationState, RuntimeProbe


def _no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)


@pytest.mark.history_mode
def test_the_arc_row_is_degraded_history_mode_with_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_binding(monkeypatch)

    row = RuntimeProbe(env={}).probe_arc()

    assert (row.name, row.state, row.required) == ("arc", IntegrationState.DEGRADED, True)
    assert row.fallback == "history"
    assert row.details["context_mode"] == "history"
    assert row.details["reason"] == "clio_core_binding_absent"
    assert "History mode" in row.summary
    assert "iowarp-core" in row.next_action


@pytest.mark.history_mode
def test_no_clio_core_row_is_down_in_history_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """No daemon is expected in History mode: neither clio-core row may read as down."""
    _no_binding(monkeypatch)
    probe = RuntimeProbe(env={}, port_checker=lambda _port: False)

    rows = [probe.probe_arc(), probe.probe_clio_core()]

    assert [(r.name, r.state) for r in rows] == [
        ("arc", IntegrationState.DEGRADED),
        ("clio_core", IntegrationState.DEGRADED),
    ]
    assert all(r.details["context_mode"] == "history" for r in rows)


@pytest.mark.history_mode
def test_health_is_degraded_not_down_and_names_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_binding(monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as client:
        response = client.get("/v1/health")

    body = response.json()
    assert response.status_code == 200, body
    assert body["context_mode"] == "history"
    [arc] = [row for row in body["integrations"] if row["name"] == "arc"]
    assert (arc["status"], arc["reason"]) == ("degraded", "clio_core_binding_absent")


def test_health_names_clio_core_mode(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as client:
        body = client.get("/v1/health").json()

    assert body["context_mode"] == "clio_core"


@pytest.mark.history_mode
def test_the_boot_records_the_context_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binding(monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit
    monkeypatch.setattr(
        app.state.semantic_event_sink, "emit", lambda e: (events.append(e), real_emit(e))[1]
    )

    assert server_boot.process_arc(app) is None

    [event] = [e for e in events if e.event_type == "context.mode"]
    assert event.payload["context_mode"] == "history"
    assert event.payload["reason"] == "clio_core_binding_absent"
