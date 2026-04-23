"""Tests for runtime integration status probes."""

from __future__ import annotations

import requests

from clio_agent.runtime.status import IntegrationState, RuntimeProbe


class FakeResponse:
    """Small response object for probe tests."""

    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body


HDF5_CAPS = [
    {"name": "hdf5_list_datasets"},
    {"name": "hdf5_analyze_dataset"},
    {"name": "hdf5_check_compression"},
    {"name": "hdf5_optimize_chunking"},
    {"name": "hdf5_analyze_file"},
]
PARQUET_CAPS = [
    {"name": "parquet_analyze_schema"},
    {"name": "parquet_query_data"},
    {"name": "parquet_compute_statistics"},
]


def test_runtime_report_ready_path(tmp_path):
    """All required integrations report ready when probes succeed."""

    def fake_get(url: str, timeout: float):
        assert url.endswith("/models")
        assert timeout == 1.0
        return FakeResponse({"data": [{"id": "granite"}]})

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=fake_get,
        gateway_lister=lambda: HDF5_CAPS + PARQUET_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet"},
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.overall_status == "ready"
    assert report.by_name("lm_provider").state == IntegrationState.READY
    assert report.by_name("arc").state == IntegrationState.READY
    assert report.by_name("gateway").state == IntegrationState.READY
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert report.by_name("parquet").state == IntegrationState.READY
    assert report.by_name("api").state == IntegrationState.READY
    assert report.by_name("clio_core").state == IntegrationState.SKIPPED


def test_runtime_report_degraded_path(tmp_path):
    """Reachable but incomplete integrations are degraded, not crashes."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=lambda *args, **kwargs: FakeResponse({"data": []}),
        gateway_lister=lambda: HDF5_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet"},
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.overall_status == "degraded"
    assert report.by_name("lm_provider").state == IntegrationState.DEGRADED
    assert report.by_name("gateway").state == IntegrationState.DEGRADED
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert report.by_name("parquet").state == IntegrationState.DEGRADED
    assert "missing" in report.by_name("parquet").summary.lower()


def test_runtime_report_unavailable_path(tmp_path):
    """Unavailable dependencies are represented as structured statuses."""

    def unavailable_lm(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    def unavailable_gateway():
        raise RuntimeError("gateway import failed")

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=unavailable_lm,
        gateway_lister=unavailable_gateway,
        module_checker=lambda name: False,
    )

    report = probe.collect(api_state=IntegrationState.DEGRADED, api_error="startup failed")

    assert report.overall_status == "degraded"
    assert report.by_name("lm_provider").state == IntegrationState.UNAVAILABLE
    assert report.by_name("gateway").state == IntegrationState.UNAVAILABLE
    assert report.by_name("hdf5").state == IntegrationState.UNAVAILABLE
    assert report.by_name("parquet").state == IntegrationState.UNAVAILABLE
    assert report.by_name("api").state == IntegrationState.DEGRADED
    assert report.by_name("api").details["error"] == "startup failed"


def test_lm_provider_misconfigured_when_cloud_key_missing(tmp_path):
    """Cloud providers without an API key are reported as misconfigured."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "openai"},
        gateway_lister=lambda: HDF5_CAPS + PARQUET_CAPS,
        module_checker=lambda name: True,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.MISCONFIGURED
    assert "requires" in status.summary
    assert "CLIO_LM_API_KEY" in status.summary
