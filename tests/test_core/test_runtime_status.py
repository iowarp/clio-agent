"""Tests for runtime integration status probes."""

from __future__ import annotations

import requests

from clio_agent.runtime.status import IntegrationState, RuntimeProbe


class FakeResponse:
    """Small response object for probe tests."""

    def __init__(self, body: object, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self) -> object:
        return self._body


class FakeInvalidJsonResponse:
    """Response object whose JSON parser fails."""

    status_code = 200

    def json(self) -> object:
        raise ValueError("not json")


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
        default_clio_core_path=None,
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.overall_status == "ready"
    assert report.by_name("lm_provider").state == IntegrationState.READY
    assert report.by_name("arc").state == IntegrationState.READY
    assert report.by_name("file_policy").state == IntegrationState.READY
    assert report.by_name("gateway").state == IntegrationState.READY
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert report.by_name("parquet").state == IntegrationState.READY
    assert report.by_name("api").state == IntegrationState.READY
    assert report.by_name("clio_core").state == IntegrationState.SKIPPED
    assert report.by_name("file_policy").details["max_file_size_bytes"] == 1 << 30


def test_runtime_report_degraded_path(tmp_path):
    """Reachable but incomplete integrations are degraded, not crashes."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=lambda *args, **kwargs: FakeResponse({"data": []}),
        gateway_lister=lambda: HDF5_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet"},
        default_clio_core_path=None,
    )

    report = probe.collect(api_state=IntegrationState.READY)

    # A gateway that mounts only HDF5 tools (no Parquet) is HEALTHY for the
    # tools it actually exposes — Parquet simply is not part of this
    # deployment and emits no status. The report is degraded only because the
    # LM provider reported no loaded models.
    assert report.overall_status == "degraded"
    assert report.by_name("lm_provider").state == IntegrationState.DEGRADED
    assert report.by_name("gateway").state == IntegrationState.READY
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert "parquet" not in {item.name for item in report.integrations}


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
        default_clio_core_path=None,
    )

    report = probe.collect(api_state=IntegrationState.DEGRADED, api_error="startup failed")

    # When gateway discovery fails, no tools are mounted, so no data-backend
    # status is emitted — backends are reported only for servers the active
    # gateway actually exposes.
    assert report.overall_status == "degraded"
    assert report.by_name("lm_provider").state == IntegrationState.UNAVAILABLE
    assert report.by_name("gateway").state == IntegrationState.UNAVAILABLE
    backend_names = {item.name for item in report.integrations}
    assert "hdf5" not in backend_names
    assert "parquet" not in backend_names
    assert report.by_name("api").state == IntegrationState.DEGRADED
    assert report.by_name("api").details["error"] == "startup failed"


def test_lm_provider_misconfigured_when_cloud_key_missing(tmp_path):
    """Cloud providers without an API key are reported as misconfigured."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "openai"},
        gateway_lister=lambda: HDF5_CAPS + PARQUET_CAPS,
        module_checker=lambda name: True,
        default_clio_core_path=None,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.MISCONFIGURED
    assert "requires" in status.summary
    assert "CLIO_LM_API_KEY" in status.summary


def test_lm_provider_probe_reports_invalid_json_as_malformed(tmp_path):
    """Doctor should not turn invalid /models JSON into a no-model status."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=lambda *args, **kwargs: FakeInvalidJsonResponse(),
        default_clio_core_path=None,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.DEGRADED
    assert "invalid" in status.summary.lower()
    assert "json" in status.summary.lower()
    assert "no loaded models" not in status.summary.lower()
    assert status.details["model_discovery_error"] == "invalid_json"


def test_lm_provider_probe_reports_malformed_schema_as_malformed(tmp_path):
    """Doctor should report schema failure when /models lacks data[]."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},
        http_get=lambda *args, **kwargs: FakeResponse({"models": [{"id": "x"}]}),
        default_clio_core_path=None,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.DEGRADED
    assert "malformed" in status.summary.lower()
    assert "data" in status.summary.lower()
    assert "no loaded models" not in status.summary.lower()
    assert status.details["model_discovery_error"] == "malformed_schema"


def test_file_policy_probe_reports_configured_roots(tmp_path):
    """Doctor exposes the effective local file access policy."""
    probe = RuntimeProbe(
        env={
            "CLIO_ALLOWED_ROOTS": str(tmp_path),
            "CLIO_MAX_FILE_SIZE_BYTES": "4096",
            "CLIO_ALLOW_SYMLINKS": "true",
        },
        default_clio_core_path=None,
    )

    status = probe.probe_file_policy()

    assert status.state == IntegrationState.READY
    assert "CLIO_ALLOWED_ROOTS" in status.config_source
    assert status.details["allowed_roots"] == [str(tmp_path.resolve())]
    assert status.details["max_file_size_bytes"] == 4096
    assert status.details["allow_symlinks"] is True


def test_file_policy_probe_reports_invalid_policy_as_misconfigured():
    """Invalid file policy env should be a doctor status, not a tool-time crash."""
    probe = RuntimeProbe(
        env={"CLIO_MAX_FILE_SIZE_BYTES": "not-an-int"},
        default_clio_core_path=None,
    )

    status = probe.probe_file_policy()

    assert status.state == IntegrationState.MISCONFIGURED
    assert "CLIO_MAX_FILE_SIZE_BYTES" in status.summary
    assert status.details["type"] == "file_policy"


def test_clio_core_probe_ready_with_default_path(tmp_path, monkeypatch):
    """Default clio-core path discovery is non-destructive and reports readiness."""
    core = tmp_path / "clio-core"
    bin_dir = core / "build" / "bin"
    bin_dir.mkdir(parents=True)
    chimaera = bin_dir / "chimaera"
    chimaera.write_text("#!/bin/sh\n", encoding="utf-8")
    chimaera.chmod(0o755)
    config_dir = core / "context-runtime" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "chimaera_default.yaml").write_text("runtime: {}\n", encoding="utf-8")
    repo_dir = core / "context-transfer-engine"
    repo_dir.mkdir(parents=True)
    (repo_dir / "chimaera_repo.yaml").write_text("repo: {}\n", encoding="utf-8")
    visualizer_dir = core / "context-visualizer"
    visualizer_dir.mkdir(parents=True)
    (visualizer_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path / "data")},
        default_clio_core_path=core,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.READY
    assert "chimaera-cli" in status.capabilities
    assert "yaml-config" in status.capabilities
    assert "chimaera-repo-config" in status.capabilities
    assert "visualizer-source" in status.capabilities
    assert status.details["non_destructive"] is True
    assert status.details["chimaera_binaries"] == [str(chimaera.resolve())]


def test_clio_core_probe_degraded_when_binary_missing(tmp_path):
    """Existing clio-core path without a chimaera binary is degraded."""
    core = tmp_path / "clio-core"
    config_dir = core / "context-runtime" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "chimaera_default.yaml").write_text("runtime: {}\n", encoding="utf-8")

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path / "data")},
        default_clio_core_path=core,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.DEGRADED
    assert "chimaera binary" in status.summary
    assert status.details["config_candidates"]


def test_clio_core_probe_checks_configured_visualizer_status(tmp_path, monkeypatch):
    """Configured visualizer URLs are checked via a non-destructive /status GET."""
    core = tmp_path / "clio-core"
    bin_dir = core / "build" / "bin"
    bin_dir.mkdir(parents=True)
    chimaera = bin_dir / "chimaera"
    chimaera.write_text("#!/bin/sh\n", encoding="utf-8")
    chimaera.chmod(0o755)
    config_dir = core / "context-runtime" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "chimaera_default.yaml").write_text("runtime: {}\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")

    def fake_get(url: str, timeout: float):
        assert url == "http://127.0.0.1:8088/status"
        assert timeout == 1.0
        return FakeResponse({"status": "ok"})

    probe = RuntimeProbe(
        env={
            "CLIO_CORE_PATH": str(core),
            "CLIO_CORE_VISUALIZER_URL": "http://127.0.0.1:8088",
        },
        http_get=fake_get,
        default_clio_core_path=None,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.READY
    assert "visualizer-status" in status.capabilities
    assert status.details["visualizer"]["status_url"] == "http://127.0.0.1:8088/status"


def test_clio_core_probe_misconfigured_for_missing_explicit_path(tmp_path):
    """Explicit missing clio-core paths are misconfigured, not skipped."""
    missing = tmp_path / "missing-core"
    probe = RuntimeProbe(
        env={"CLIO_CORE_PATH": str(missing)},
        default_clio_core_path=None,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.MISCONFIGURED
    assert str(missing) in status.summary


def test_clio_core_probe_misconfigured_for_missing_env_config(tmp_path):
    """Configured clio-core env paths must point at existing files or dirs."""
    core = tmp_path / "clio-core"
    core.mkdir()
    missing_conf = tmp_path / "missing.yaml"
    probe = RuntimeProbe(
        env={"CLIO_CORE_PATH": str(core), "CHI_SERVER_CONF": str(missing_conf)},
        default_clio_core_path=None,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.MISCONFIGURED
    assert "CHI_SERVER_CONF" in status.summary
    assert status.details["env"][0]["exists"] is False
