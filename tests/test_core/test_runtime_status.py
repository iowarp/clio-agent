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


def test_runtime_report_ready_path(tmp_path, monkeypatch):
    """All required integrations report ready when probes succeed."""

    # The clio-core health rows locate the shared daemon by port; a daemon left running
    # on this machine (default port) made this report "degraded" (found 2026-10-01).
    import socket

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        monkeypatch.setenv("CLIO_CORE_PORT", str(free.getsockname()[1]))

    def fake_get(url: str, timeout: float):
        assert url.endswith("/models")
        assert timeout == 1.0
        return FakeResponse({"data": [{"id": "granite"}]})

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "cte"},
        http_get=fake_get,
        gateway_lister=lambda: HDF5_CAPS + PARQUET_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet", "iowarp_core"},
        port_checker=lambda port: True,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.overall_status == "ready"
    assert report.by_name("lm_provider").state == IntegrationState.READY
    assert report.by_name("arc").state == IntegrationState.READY
    assert report.by_name("arc").details["storage_mode"] == "cte"
    assert report.by_name("file_policy").state == IntegrationState.READY
    assert report.by_name("gateway").state == IntegrationState.READY
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert report.by_name("parquet").state == IntegrationState.READY
    assert report.by_name("api").state == IntegrationState.READY
    assert report.by_name("clio_core").state == IntegrationState.READY
    assert report.by_name("file_policy").details["max_file_size_bytes"] == 1 << 30


def test_runtime_report_degraded_path(tmp_path):
    """Reachable but incomplete integrations are degraded, not crashes."""
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "cte"},
        http_get=lambda *args, **kwargs: FakeResponse({"data": []}),
        gateway_lister=lambda: HDF5_CAPS,
        module_checker=lambda name: name in {"h5py", "pyarrow.parquet", "iowarp_core"},
        port_checker=lambda port: True,
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
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_ARC_STORE": "cte"},
        http_get=unavailable_lm,
        gateway_lister=unavailable_gateway,
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: True,
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


def _http_get_must_not_run(*args, **kwargs):
    raise AssertionError("HTTP GET must not run for a CLI/SDK pseudo-scheme provider (#899)")


def test_lm_provider_sdk_transport_requires_live_verification_when_cli_present(
    tmp_path, monkeypatch
):
    """Installed Claude support is not READY until a live provider check succeeds.

    The probe must be transport-aware: it never HTTP-GETs the pseudo-scheme (which
    yields 'No connection adapters'); it probes the CLI the transport spawns.
    """
    import shutil

    from clio_agent.providers.components import client_binary
    from clio_agent.runtime import lm_provider_probe

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        lm_provider_probe.importlib.util,
        "find_spec",
        lambda name: object() if name == "claude_agent_sdk" else None,
    )
    # "CLI present": the ONE claude selection the transport and discovery share
    # reports a runnable CLI (independent of what this host has installed).
    monkeypatch.setattr(
        client_binary,
        "claude_client",
        lambda: client_binary.ClientSelection(
            client_binary.ClientBinary("/usr/bin/claude", "2.1.281", "installed"),
            "claude_bundled_missing",
        ),
    )
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "claude_code"},
        http_get=_http_get_must_not_run,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["reason"] == "auth_check_required"
    assert status.details["transport"] == "sdk"
    assert status.details["cli_binary"] == "claude"


def test_lm_provider_sdk_transport_unavailable_when_sdk_package_absent(tmp_path, monkeypatch):
    """Claude SDK transport must not report READY when its Python package is absent."""
    import shutil

    from clio_agent.runtime import lm_provider_probe

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(lm_provider_probe.importlib.util, "find_spec", lambda _name: None)
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "claude_code"},
        http_get=_http_get_must_not_run,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["reason"] == "sdk_package_absent"
    assert "Install Claude Code support" in status.next_action


def test_lm_provider_sdk_transport_unavailable_when_cli_absent(tmp_path, monkeypatch):
    """SDK transport -> typed UNAVAILABLE (not an HTTP error) when the CLI is absent (#899)."""
    import shutil

    from clio_agent.providers.components import client_binary
    from clio_agent.runtime import lm_provider_probe

    monkeypatch.setattr(shutil, "which", lambda binary: None)
    # "CLI absent": the shared claude selection finds none (independent of this host).
    monkeypatch.setattr(
        client_binary,
        "claude_client",
        lambda: client_binary.ClientSelection(None, "claude_no_cli"),
    )
    monkeypatch.setattr(
        lm_provider_probe.importlib.util,
        "find_spec",
        lambda name: object() if name == "claude_agent_sdk" else None,
    )
    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "claude_code"},
        http_get=_http_get_must_not_run,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["reason"] == "cli_binary_absent"
    assert "No connection adapters" not in status.summary


def test_codex_doctor_uses_credential_store_not_path(tmp_path, monkeypatch):
    """Codex readiness follows its signed-in-credential contract, not a CLI on PATH.

    The direct Codex provider has no CLI binary or SDK dependency -- the local
    readiness signal is a usable sign-in (CLIO's own credential here,
    ``CodexCredentialStore.is_signed_in``; else the CLI's ``$CODEX_HOME/auth.json``).
    """
    from clio_agent.providers.codex.credentials import CodexCredentialStore

    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: True)
    monkeypatch.setattr("shutil.which", lambda _binary: None)

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "codex"},
        http_get=_http_get_must_not_run,
    )
    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["provider"] == "codex"
    assert status.details["reason"] == "auth_unverified"


def test_codex_doctor_reports_missing_auth(tmp_path, monkeypatch):
    from clio_agent.providers.codex.credentials import CodexCredentialStore

    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: False)

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path), "CLIO_LM_PROVIDER": "codex"},
        http_get=_http_get_must_not_run,
    )
    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.details["reason"] == "auth_absent"


def test_codex_sdk_doctor_does_not_use_direct_credentials(tmp_path, monkeypatch):
    """A working SDK uses its own credential home, not CLIO direct credentials."""
    from clio_agent.providers.codex.credentials import CodexCredentialStore
    from clio_agent.runtime import lm_provider_probe

    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: False)
    monkeypatch.setattr(
        lm_provider_probe.importlib.util,
        "find_spec",
        lambda name: object() if name == "openai_codex" else None,
    )
    probe = RuntimeProbe(
        env={
            "CLIO_DATA_DIR": str(tmp_path),
            "CLIO_LM_PROVIDER": "codex",
            "CLIO_CODEX_VARIANT": "sdk",
        },
        http_get=_http_get_must_not_run,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.DEGRADED
    assert status.details["reason"] == "auth_check_required"
    assert status.details["transport"] == "sdk"


def test_health_probe_uses_active_codex_variant(monkeypatch):
    """The GACT health row must diagnose the active SDK rather than direct auth."""
    from clio_agent.gact.routes.provider_probe_env import runtime_provider_probe_env

    monkeypatch.setenv("CLIO_CODEX_VARIANT", "direct")
    env = runtime_provider_probe_env(
        {"provider": "codex", "model": "gpt-6-luna", "codex_variant": "sdk"}
    )

    assert env["CLIO_CODEX_VARIANT"] == "sdk"


def test_lm_provider_http_provider_still_probes_models(tmp_path):
    """HTTP providers are unchanged: the /models GET path still drives the row (#899)."""
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float):
        seen["url"] = url
        return FakeResponse({"data": [{"id": "granite"}]})

    probe = RuntimeProbe(
        env={"CLIO_DATA_DIR": str(tmp_path)},  # default provider is lm_studio (HTTP)
        http_get=fake_get,
    )

    status = probe.probe_lm_provider()

    assert status.state == IntegrationState.READY
    assert seen["url"].endswith("/models")


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


def _crash_record(state_dir):
    from clio_agent.arc.runtime_crash import crash_record_path

    crash_record_path(state_dir).write_text('{"exit_code": 3221225477}', encoding="utf-8")


def _attach_phase(monkeypatch, phase):
    from clio_agent.arc import clio_core_attach
    from clio_agent.runtime import clio_core_health

    snap = clio_core_attach.ClioCoreAttachState(phase=phase, reason=f"clio_core_{phase.value}")
    monkeypatch.setattr(clio_core_health, "attach_state_snapshot", lambda: snap)


def test_a_daemon_not_started_yet_is_starting_not_down(tmp_path, monkeypatch):
    """Found live: a fresh server answered /v1/health 503 before its first agent build
    spawned clio-core. CLIO starts the daemon on first use: that is DEGRADED, never down."""
    from clio_agent.arc.clio_core_attach import ClioCoreAttachPhase

    _attach_phase(monkeypatch, ClioCoreAttachPhase.IDLE)
    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    rows = [probe.probe_arc(), probe.probe_clio_core()]

    assert [(r.name, r.state) for r in rows] == [
        ("arc", IntegrationState.DEGRADED),
        ("clio_core", IntegrationState.DEGRADED),
    ]
    assert {r.details["reason"] for r in rows} == {"clio_core_starting"}


def test_a_failed_attach_with_no_daemon_is_down(tmp_path, monkeypatch):
    from clio_agent.arc.clio_core_attach import ClioCoreAttachPhase

    _attach_phase(monkeypatch, ClioCoreAttachPhase.UNAVAILABLE)
    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    assert probe.probe_arc().state == IntegrationState.UNAVAILABLE


def test_arc_clio_core_default_backend_red_when_daemon_down(tmp_path):
    """Default backend is clio-core: iowarp_core installed but no daemon MUST go red."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    (clio_home / "clio-runtime.log").write_text(
        "boot: composing pools\nFATAL: could not bind RPC port\n", encoding="utf-8"
    )
    _crash_record(clio_home)

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
    assert status.details["reason"] == "clio_core_daemon_not_listening"
    assert isinstance(status.details["port"], int)
    assert any("FATAL" in line for line in status.details["log_tail"])
    assert "clio-runtime.log" in status.details["log_path"]


def test_arc_clio_core_backend_ready_when_daemon_listening(tmp_path):
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


def test_clio_core_red_when_clio_core_backend_and_daemon_down(tmp_path):
    """With the default clio-core backend a dead daemon turns the report red."""
    clio_home = tmp_path / "clio-home"
    clio_home.mkdir()
    (clio_home / "clio-runtime.log").write_text("FATAL: shm init failed\n", encoding="utf-8")
    _crash_record(clio_home)

    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: False,
        clio_runtime_dir=clio_home,
    )

    status = probe.probe_clio_core()

    assert status.state == IntegrationState.UNAVAILABLE
    assert status.required is True
    assert status.details["reason"] == "clio_core_daemon_not_listening"
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
