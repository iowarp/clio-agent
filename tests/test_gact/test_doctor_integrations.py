"""#800 — /v1/health is the ONE doctor: the RuntimeProbe engine.

Since the "one front door" unification, ``GET /v1/health`` delegates to the same
:func:`clio_agent.runtime.status.collect_runtime_status` probe engine the CLI
renders, instead of hand-rolling five rows off ``app.state``. These tests pin the
new richer rows, the 503-on-``unavailable`` contract *through the gact surface*,
and the cached-LM-handshake fold — driving the deterministic probe branches via
the ``RuntimeProbe`` injection points and swapping the engine into the route with
monkeypatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)
from clio_agent.runtime.status import RuntimeProbe

HDF5_CAPS = [
    {"name": "hdf5_list_datasets"},
    {"name": "hdf5_analyze_dataset"},
]
PARQUET_CAPS = [
    {"name": "parquet_analyze_schema"},
    {"name": "parquet_query_data"},
]


class _FakeResponse:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> object:
        return self._body


def _patch_engine(monkeypatch: pytest.MonkeyPatch, probe: RuntimeProbe) -> None:
    """Swap the route's probe engine for one built from injected fakes.

    The health handler calls ``collect_runtime_status(api_state=..., lm_timeout=...)``;
    this replacement funnels that into a pre-wired ``RuntimeProbe`` so a down clio-core
    daemon / unreachable dependency is deterministic in-process.
    """

    def _fake(
        *,
        api_state: Any = None,
        api_error: str | None = None,
        env: Any = None,
        lm_timeout: float = 1.0,
        include_process_census: bool = True,
    ):
        return probe.collect(
            api_state=api_state, api_error=api_error, include_process_census=include_process_census
        )

    monkeypatch.setattr("clio_agent.gact.routes.system.collect_runtime_status", _fake)


def _ready_probe(tmp_path: Path, **overrides: Any) -> RuntimeProbe:
    """A fully-ready probe (local ARC backend, models loaded, tools mounted)."""

    kwargs: dict[str, Any] = {
        "env": {"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "local"},
        "http_get": lambda *a, **k: _FakeResponse({"data": [{"id": "granite"}]}),
        "gateway_lister": lambda: HDF5_CAPS + PARQUET_CAPS,
        "module_checker": lambda name: name in {"h5py", "pyarrow.parquet"},
        "port_checker": lambda port: False,
        "clio_runtime_dir": tmp_path / "clio-home",
    }
    kwargs.update(overrides)
    return RuntimeProbe(**kwargs)


def _health(app: Any, monkeypatch: pytest.MonkeyPatch, probe: RuntimeProbe):
    _patch_engine(monkeypatch, probe)
    return TestClient(app).get("/v1/health")


def _rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in body["integrations"]}


def test_health_returns_probe_engine_rows_not_hand_rolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rich probe rows replace the old api/sessions/agent/memory/lm five."""
    resp = _health(
        build_app(sessions_path=tmp_path / "s.json"),
        monkeypatch,
        _ready_probe(tmp_path),
    )
    assert resp.status_code == 200
    body = resp.json()
    rows = _rows(body)
    # The unified doctor's rich set:
    assert {"lm_provider", "arc", "file_policy", "gateway", "api"} <= set(rows)
    # Data backends mounted on the (injected) gateway are probed too:
    assert {"hdf5", "parquet"} <= set(rows)
    # The old hand-rolled rows are gone:
    assert "sessions" not in rows
    assert "agent" not in rows
    assert "memory" not in rows
    # The fixture selects the LOCAL ARC backend, which is DEGRADED by policy
    # (underperforming fallback, owner ruling 2026-07-14) — never fully ready.
    assert rows["arc"]["status"] == "degraded"
    assert body["overall_status"] == "degraded"
    assert body["healthy"] is True


def test_widened_rows_carry_full_doctor_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row exposes the render_doctor_report columns (summary/source/action)."""
    resp = _health(
        build_app(sessions_path=tmp_path / "s.json"),
        monkeypatch,
        _ready_probe(tmp_path),
    )
    rows = _rows(resp.json())
    fp = rows["file_policy"]
    # Back-compat triple + additive richer fields (no probe detail lost).
    assert fp["status"] == "ready"
    assert fp["detail"] == fp["summary"]  # detail mirrors summary
    assert fp["summary"]
    assert fp["config_source"]
    assert fp["next_action"]
    arc = rows["arc"]
    assert arc["endpoint"]  # local arc dir surfaces as endpoint


def test_down_clio_core_daemon_turns_health_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clio-core backend + installed pkg + daemon NOT listening -> arc red -> 503."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    probe = _ready_probe(
        tmp_path,
        env={"CLIO_ARC_STORE": "cte"},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,  # daemon down
        clio_runtime_dir=clio_home,
    )
    resp = _health(build_app(sessions_path=tmp_path / "s.json"), monkeypatch, probe)
    assert resp.status_code == 503
    body = resp.json()
    assert body["overall_status"] == "unavailable"
    assert body["healthy"] is False
    rows = _rows(body)
    assert rows["arc"]["status"] == "unavailable"
    assert rows["clio_core"]["status"] == "unavailable"


def test_unreachable_lm_dependency_turns_health_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A required dependency going UNAVAILABLE reds the surface (503)."""

    def _refused(*a: Any, **k: Any):
        raise requests.ConnectionError("connection refused")

    probe = _ready_probe(tmp_path, http_get=_refused)
    resp = _health(build_app(sessions_path=tmp_path / "s.json"), monkeypatch, probe)
    assert resp.status_code == 503
    body = resp.json()
    assert body["overall_status"] == "unavailable"
    assert _rows(body)["lm_provider"]["status"] == "unavailable"


def test_degraded_dependency_is_200_not_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No loaded models -> lm degraded, but nothing unavailable -> 200 degraded."""
    probe = _ready_probe(tmp_path, http_get=lambda *a, **k: _FakeResponse({"data": []}))
    resp = _health(build_app(sessions_path=tmp_path / "s.json"), monkeypatch, probe)
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "degraded"
    assert body["healthy"] is True
    assert _rows(body)["lm_provider"]["status"] == "degraded"


def test_lm_row_carries_cached_handshake_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached handshake folds into the lm_provider row (never runs one)."""
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.lm_handshake_report = HandshakeReport(
        provider_id="lm_studio",
        provider_kind="lm_studio",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models=(ModelProfile(id="qwen"), ModelProfile(id="granite")),
    )
    resp = _health(app, monkeypatch, _ready_probe(tmp_path))
    assert resp.status_code == 200
    lm = _rows(resp.json())["lm_provider"]
    assert lm["summary"] == "2 model(s) discovered"
    assert lm["config_source"] == "handshake"


def test_no_cached_handshake_leaves_probe_lm_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent a cached handshake the env-probe lm row is used unchanged."""
    app = build_app(sessions_path=tmp_path / "s.json")
    # Ensure no cached report is present.
    if hasattr(app.state, "lm_handshake_report"):
        app.state.lm_handshake_report = None
    resp = _health(app, monkeypatch, _ready_probe(tmp_path))
    lm = _rows(resp.json())["lm_provider"]
    # The env probe's config_source, not the handshake's.
    assert lm["config_source"] != "handshake"
    assert lm["status"] == "ready"


def test_stale_ready_handshake_does_not_mask_live_down_lm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a cached handshake is a CACHE from an earlier successful bind — it must
    never flip a live-UNAVAILABLE lm_provider probe back to healthy. A stale READY handshake
    over a now-down provider keeps the live-down state (and the 503), instead of masking it."""

    def _refused(*a: Any, **k: Any):
        raise requests.ConnectionError("connection refused")

    app = build_app(sessions_path=tmp_path / "s.json")
    # An earlier successful bind cached a healthy handshake...
    app.state.lm_handshake_report = HandshakeReport(
        provider_id="lm_studio",
        provider_kind="lm_studio",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models=(ModelProfile(id="qwen"),),
    )
    # ...but the live probe now finds the provider unreachable.
    resp = _health(app, monkeypatch, _ready_probe(tmp_path, http_get=_refused))
    assert resp.status_code == 503  # NOT masked back to 200 by the stale handshake
    body = resp.json()
    assert body["overall_status"] == "unavailable"
    assert _rows(body)["lm_provider"]["status"] == "unavailable"


def test_handshake_enrichment_failure_is_logged_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: if the cached handshake's to_integration_status() raises, the fold must LOG
    (non-silent) and fall back to the un-enriched probe lm_provider row — not vanish."""
    import logging

    class _BoomHandshake:
        def to_integration_status(self) -> Any:
            raise RuntimeError("handshake render exploded")

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.lm_handshake_report = _BoomHandshake()
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.routes.system"):
        resp = _health(app, monkeypatch, _ready_probe(tmp_path))
    assert resp.status_code == 200
    # The un-enriched env-probe row is served (config_source is not the handshake's).
    assert _rows(resp.json())["lm_provider"]["config_source"] != "handshake"
    # ...and the failure reached the logs rather than being swallowed.
    assert any("handshake enrichment failed" in r.message for r in caplog.records)


def test_probe_engine_failure_is_structured_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe-engine crash surfaces a structured degraded row (no bare 200)."""

    def _boom(**kwargs: Any):
        raise RuntimeError("probe engine exploded")

    monkeypatch.setattr("clio_agent.gact.routes.system.collect_runtime_status", _boom)
    resp = TestClient(build_app(sessions_path=tmp_path / "s.json")).get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "degraded"
    rows = _rows(body)
    assert rows["doctor"]["status"] == "degraded"
    assert "probe engine exploded" in rows["doctor"]["summary"]


# Guard against accidentally re-introducing a blocking handshake in the polled
# health path: patch run_handshake to explode and assert /v1/health never calls it.
def test_health_never_runs_a_handshake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {"ran": False}

    def _forbidden(*a: Any, **k: Any):
        called["ran"] = True
        raise AssertionError("health must not run a live handshake")

    # run_handshake is imported lazily inside the providers route; patch the source.
    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _forbidden, raising=False)
    resp = _health(
        build_app(sessions_path=tmp_path / "s.json"), monkeypatch, _ready_probe(tmp_path)
    )
    assert resp.status_code == 200
    assert called["ran"] is False
