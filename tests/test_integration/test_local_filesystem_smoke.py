"""Local filesystem smoke tests for the integration-ready harness.

Scope note: the in-process HDF5/Parquet/CSV/plot domain servers were removed
from core (commits afc7c8a / c8014e2 / a845177 — "de-case core") and now live
in clio-kit as declared MCP servers. The smoke tests that drove those
in-process tools (``hdf5_analyze_file``, ``parquet_analyze_schema``,
``csv_read_table``, ``plot_summary``) were deleted along with the servers; what
remains here is the core, domain-free behaviour: the local file policy / gateway
truth and agent-construction hygiene.
"""

from __future__ import annotations

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.runtime.status import IntegrationState, RuntimeProbe

pytestmark = pytest.mark.integration


def test_doctor_reports_local_file_policy_and_tool_backends(tmp_path):
    """Doctor should report runtime truth for local-filesystem mode."""

    def fake_get(url: str, timeout: float):
        assert url.endswith("/models")
        return type(
            "FakeResponse",
            (),
            {"status_code": 200, "json": lambda self: {"data": [{"id": "granite"}]}},
        )()

    probe = RuntimeProbe(
        env={
            "CLIO_DATA_DIR": str(tmp_path / "data"),
            "CLIO_ALLOWED_ROOTS": str(tmp_path),
            "CLIO_ARC_STORE": "local",
        },
        http_get=fake_get,
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.by_name("file_policy").state == IntegrationState.READY
    assert report.by_name("file_policy").details["allowed_roots"] == [str(tmp_path.resolve())]
    # The default gateway mounts only the universal fs/shell built-ins, so it
    # is HEALTHY without HDF5/Parquet — those domain servers moved to clio-kit
    # and are reported only when actually mounted. A clean core must not be
    # marked degraded.
    gateway = report.by_name("gateway")
    assert gateway.state == IntegrationState.READY
    assert gateway.capabilities
    backend_names = {item.name for item in report.integrations}
    assert "hdf5" not in backend_names
    assert "parquet" not in backend_names


def test_agent_init_suppresses_model_selection_output(tmp_path, monkeypatch, capsys):
    """Non-verbose agent construction must not pollute CLI JSON output."""
    import clio_agent.agent as agent_module

    monkeypatch.delenv("CLIO_LM_PROVIDER", raising=False)
    monkeypatch.setattr(agent_module, "list_lm_studio_models", lambda **_: ["granite"])

    def noisy_selector(models):
        print("noisy model selection")
        return "granite", "granite"

    monkeypatch.setattr(agent_module, "select_models_for_agents", noisy_selector)

    agent = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    try:
        output = capsys.readouterr().out
    finally:
        agent.shutdown()

    assert "noisy model selection" not in output
