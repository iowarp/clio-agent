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

GACT_HEALTH_READY = {
    "healthy": True,
    "uptime_s": 12,
    "overall_status": "ready",
    "integrations": [{"name": "api", "status": "ready", "detail": "ok"}],
}
GACT_CAPABILITIES = {
    "contract_version": "0.2",
    "backend": {"name": "clio-agent-gact", "version": "1.2.3"},
    "capabilities": {
        "sessions": True,
        "metrics": True,
        "memory": True,
        "session_summary": False,
        "x_clio_cancellation": "best_effort",
    },
}


def test_runtime_report_ready_path(tmp_path):
    """All required integrations report ready when probes succeed."""

    def fake_get(url: str, timeout: float):
        assert url.endswith("/models")
        assert timeout == 1.0
        return FakeResponse({"data": [{"id": "granite"}]})

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "local"},
        http_get=fake_get,
        gateway_lister=lambda: HDF5_CAPS + PARQUET_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet"},
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.overall_status == "ready"
    assert report.by_name("lm_provider").state == IntegrationState.READY
    assert report.by_name("arc").state == IntegrationState.READY
    assert report.by_name("arc").details["storage_mode"] == "local"
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
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "local"},
        http_get=lambda *args, **kwargs: FakeResponse({"data": []}),
        gateway_lister=lambda: HDF5_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet"},
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
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
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "local"},
        http_get=unavailable_lm,
        gateway_lister=unavailable_gateway,
        module_checker=lambda name: False,
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
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
    )

    status = probe.probe_file_policy()

    assert status.state == IntegrationState.MISCONFIGURED
    assert "CLIO_MAX_FILE_SIZE_BYTES" in status.summary
    assert status.details["type"] == "file_policy"


# ---------------------------------------------------------------------------
# probe_arc — the ACTUAL selected backend (mirrors make_arc_store), not a
# hardcoded 'local' (#800).
# ---------------------------------------------------------------------------


def test_arc_cte_default_backend_red_when_daemon_down(tmp_path):
    """Default backend is CTE: iowarp_core installed but no daemon MUST go red."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    (clio_home / "clio-runtime.log").write_text(
        "boot: composing pools\nFATAL: could not bind RPC port\n", encoding="utf-8"
    )

    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=clio_home,
    )

    status = probe.probe_arc()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.required is True
    assert status.details["storage_mode"] == "cte"
    assert status.details["reason"] == "cte_daemon_not_listening"
    assert isinstance(status.details["port"], int)
    assert any("FATAL" in line for line in status.details["log_tail"])
    assert "clio-runtime.log" in status.details["log_path"]


def test_arc_cte_backend_red_when_iowarp_core_missing(tmp_path):
    """CTE selected but the pip runtime is absent: a broken install goes red."""
    probe = RuntimeProbe(
        env={"CLIO_ARC_STORE": "cte"},
        module_checker=lambda name: False,
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_arc()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["storage_mode"] == "cte"
    assert status.details["reason"] == "iowarp_core_not_installed"
    assert "iowarp" in status.summary.lower()


def test_arc_cte_backend_ready_when_daemon_listening(tmp_path):
    """Installed pip runtime + listening daemon reports READY with cte mode."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    (clio_home / "clio-runtime.pid").write_text("4242 1234.5", encoding="utf-8")
    seen_ports: list[int] = []

    def port_checker(port: int) -> bool:
        seen_ports.append(port)
        return True

    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=port_checker,
        clio_runtime_dir=clio_home,
    )

    status = probe.probe_arc()

    assert status.state == IntegrationState.READY
    assert status.details["storage_mode"] == "cte"
    assert status.details["daemon_alive"] is True
    assert status.details["daemon_pid"] == 4242
    assert seen_ports and status.details["port"] == seen_ports[0]


def test_arc_local_backend_keeps_writability_check(tmp_path):
    """Explicit local backend keeps the writable-directory probe."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "local"},
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_arc()

    assert status.state == IntegrationState.READY
    assert status.details["storage_mode"] == "local"
    assert (tmp_path / "arc").is_dir()


def test_arc_unknown_backend_is_misconfigured(tmp_path):
    """An unknown CLIO_ARC_STORE value is a structured misconfiguration."""
    probe = RuntimeProbe(
        env={"CLIO_ARC_STORE": "weird"},
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_arc()

    assert status.state == IntegrationState.MISCONFIGURED
    assert status.details["reason"] == "unknown_arc_backend"
    assert "weird" in status.summary


# ---------------------------------------------------------------------------
# probe_clio_core — production probe is the pip runtime + shared daemon,
# shared with probe_arc (#800). The source-repo layout probe is retired.
# ---------------------------------------------------------------------------


def test_clio_core_red_when_cte_backend_and_daemon_down(tmp_path):
    """With the default CTE backend a dead daemon turns the report red."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    (clio_home / "clio-runtime.log").write_text("FATAL: shm init failed\n", encoding="utf-8")

    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=clio_home,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.required is True
    assert status.details["reason"] == "cte_daemon_not_listening"
    assert any("FATAL" in line for line in status.details["log_tail"])

    report = probe.collect(api_state=IntegrationState.READY)
    assert report.overall_status == "degraded"


def test_clio_core_ready_when_daemon_listening(tmp_path):
    """Installed pip runtime + listening daemon is READY."""
    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: True,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.READY
    assert status.endpoint is not None
    assert str(status.details["port"]) in status.endpoint


def test_clio_core_skipped_when_local_backend_and_not_installed(tmp_path):
    """Local ARC backend without the pip runtime: clio-core is simply not used."""
    probe = RuntimeProbe(
        env={"CLIO_ARC_STORE": "local"},
        module_checker=lambda name: False,
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.SKIPPED
    assert status.required is False


def test_clio_core_optional_when_local_backend_and_daemon_down(tmp_path):
    """Installed runtime with a dead daemon is reported, but not required on local."""
    probe = RuntimeProbe(
        env={"CLIO_ARC_STORE": "local"},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.required is False
    assert status.details["reason"] == "cte_daemon_not_listening"


# ---------------------------------------------------------------------------
# probe_api — the gact /v1 surface (/v1/health + /v1/capabilities), not the
# legacy /health /query /experts endpoints (#800).
# ---------------------------------------------------------------------------


def test_api_probe_targets_gact_v1_surface():
    """A healthy gact server yields READY with the capability summary."""
    calls: list[str] = []

    def fake_get(url: str, timeout: float):
        calls.append(url)
        if url.endswith("/v1/health"):
            return FakeResponse(GACT_HEALTH_READY)
        if url.endswith("/v1/capabilities"):
            return FakeResponse(GACT_CAPABILITIES)
        raise AssertionError(f"unexpected probe URL: {url}")

    probe = RuntimeProbe(
        env={"CLIO_API_BASE": "http://127.0.0.1:17800"},
        http_get=fake_get,
    )

    status = probe.probe_api()

    assert calls == [
        "http://127.0.0.1:17800/v1/health",
        "http://127.0.0.1:17800/v1/capabilities",
    ]
    assert status.state == IntegrationState.READY
    assert status.details["contract_version"] == "0.2"
    assert status.details["backend"] == {"name": "clio-agent-gact", "version": "1.2.3"}
    assert status.details["capabilities_enabled"] == ["memory", "metrics", "sessions"]
    assert status.capabilities == ["memory", "metrics", "sessions"]
    assert "0.2" in status.summary


def test_api_probe_red_when_gact_down():
    """An unreachable gact server MUST turn the report red."""

    def refused(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    probe = RuntimeProbe(
        env={
            "CLIO_API_BASE": "http://127.0.0.1:17800",
            "CLIO_ARC_STORE": "local",
            "CLIO_DATA_DIR": "unused",
        },
        http_get=refused,
        gateway_lister=lambda: HDF5_CAPS,
        module_checker=lambda name: name in {"h5py"},
        port_checker=lambda port: False,
    )

    status = probe.probe_api()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["reason"] == "gact_unreachable"
    assert "/v1/health" in status.summary


def test_api_probe_degraded_when_gact_reports_degraded():
    """gact /v1/health overall_status=degraded maps to a degraded probe."""

    def fake_get(url: str, timeout: float):
        assert url.endswith("/v1/health")
        return FakeResponse(
            {
                "healthy": True,
                "overall_status": "degraded",
                "integrations": [
                    {"name": "api", "status": "ready"},
                    {"name": "lm", "status": "degraded"},
                ],
            }
        )

    probe = RuntimeProbe(
        env={"CLIO_API_BASE": "http://127.0.0.1:17800"},
        http_get=fake_get,
    )

    status = probe.probe_api()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["reason"] == "gact_unhealthy"
    assert status.details["health_status"] == "degraded"
    assert status.details["unhealthy_integrations"] == ["lm"]


def test_api_probe_unavailable_when_gact_health_503():
    """gact returns 503 + overall_status=unavailable when it cannot serve."""

    def fake_get(url: str, timeout: float):
        assert url.endswith("/v1/health")
        return FakeResponse(
            {"healthy": False, "overall_status": "unavailable", "integrations": []},
            status_code=503,
        )

    probe = RuntimeProbe(
        env={"CLIO_API_BASE": "http://127.0.0.1:17800"},
        http_get=fake_get,
    )

    status = probe.probe_api()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["reason"] == "gact_unhealthy"
    assert status.details["http_status"] == 503


def test_api_probe_degraded_when_capabilities_unreachable():
    """Healthy /v1/health but a failing /v1/capabilities is degraded, with reason."""

    def fake_get(url: str, timeout: float):
        if url.endswith("/v1/health"):
            return FakeResponse(GACT_HEALTH_READY)
        raise requests.ConnectionError("capabilities refused")

    probe = RuntimeProbe(
        env={"CLIO_API_BASE": "http://127.0.0.1:17800"},
        http_get=fake_get,
    )

    status = probe.probe_api()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["reason"] == "gact_capabilities_unavailable"


def test_api_probe_skipped_without_endpoint():
    """No CLIO_API_BASE and no in-process state: probe is skipped, not invented."""
    probe = RuntimeProbe(env={})

    status = probe.probe_api()

    assert status.state == IntegrationState.SKIPPED
    assert "/v1/health" in status.capabilities
