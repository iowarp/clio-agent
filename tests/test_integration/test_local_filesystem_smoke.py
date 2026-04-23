"""Local filesystem smoke tests for the integration-ready harness."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from clio_agent.agent import ClioAgent
from clio_agent.runtime.status import IntegrationState, RuntimeProbe
from clio_agent.tools.gateway import gateway

pytestmark = pytest.mark.integration


@pytest.fixture
def sample_csv(tmp_path: Path) -> str:
    filepath = tmp_path / "test_data.csv"
    filepath.write_text(
        "\n".join(
            [
                "sample_id,temperature,pressure,site",
                "0,281.5,101100,north",
                "1,284.2,101600,south",
                "2,279.8,100900,east",
                "3,290.1,101900,west",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return str(filepath)


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
        },
        http_get=fake_get,
        default_clio_core_path=None,
    )

    report = probe.collect(api_state=IntegrationState.READY)

    assert report.by_name("file_policy").state == IntegrationState.READY
    assert report.by_name("file_policy").details["allowed_roots"] == [str(tmp_path.resolve())]
    assert report.by_name("gateway").state == IntegrationState.READY
    assert report.by_name("hdf5").state == IntegrationState.READY
    assert report.by_name("parquet").state == IntegrationState.READY


@pytest.mark.asyncio
async def test_gateway_tools_read_real_local_hdf5_and_parquet(
    sample_hdf5,
    sample_parquet,
    tmp_path,
    monkeypatch,
):
    """Gateway calls should inspect real local fixtures under the file policy."""
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    async with Client(gateway) as client:
        hdf5_result = await client.call_tool("hdf5_analyze_file", {"filepath": sample_hdf5})
        parquet_result = await client.call_tool(
            "parquet_analyze_schema", {"filepath": sample_parquet}
        )

    assert hdf5_result.data["total_datasets"] == 3
    assert "simulation/temperature" in hdf5_result.data["datasets"]
    assert parquet_result.data["num_rows"] == 100
    assert parquet_result.data["num_columns"] == 3


def test_agent_init_suppresses_model_selection_output(tmp_path, monkeypatch, capsys):
    """Non-verbose agent construction must not pollute CLI JSON output."""
    import clio_agent.agent as agent_module

    monkeypatch.delenv("CLIO_LM_PROVIDER", raising=False)
    monkeypatch.setattr(agent_module, "fetch_lm_studio_models", lambda **_: ["granite"])

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


def test_direct_agent_answers_for_local_hdf5_parquet_csv(
    sample_hdf5,
    sample_parquet,
    sample_csv,
    tmp_path,
    monkeypatch,
):
    """Explicit local file questions should use deterministic tool-backed answers."""
    monkeypatch.setenv("CLIO_LM_PROVIDER", "ollama")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    agent = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    try:
        hdf5 = agent(
            question=f"What datasets are in {sample_hdf5}?",
            session_id="local-hdf5",
        )
        parquet = agent(
            question=f"Show parquet statistics for temperature in {sample_parquet}",
            session_id="local-parquet",
        )
        csv = agent(
            question=f"Inspect {sample_csv}",
            session_id="local-csv",
        )
        data_invocations = agent.arc.get_invocations_by_agent("data")
        analysis_invocations = agent.arc.get_invocations_by_agent("analysis")
    finally:
        agent.shutdown()

    assert hdf5.selected_expert == "data"
    assert "simulation/temperature" in hdf5.answer
    assert parquet.selected_expert == "analysis"
    assert "Column statistics" in parquet.answer
    assert "temperature" in parquet.answer
    assert csv.selected_expert == "analysis"
    assert "Inspected CSV file" in csv.answer
    assert "temperature" in csv.answer
    assert any(
        tool.tool == "hdf5_analyze_file"
        for invocation in data_invocations
        for tool in invocation.tools_called
    )
    assert any(
        tool.tool == "parquet_analyze_schema"
        for invocation in analysis_invocations
        for tool in invocation.tools_called
    )
    assert any(
        tool.tool == "csv_read_table"
        for invocation in analysis_invocations
        for tool in invocation.tools_called
    )


def test_api_query_wraps_real_local_agent_path(sample_hdf5, tmp_path, monkeypatch):
    """The API query endpoint should preserve deterministic local file answers."""
    from clio_agent.config import LMProviderConfig
    from clio_agent.ui.api import app

    monkeypatch.setenv("CLIO_LM_PROVIDER", "ollama")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", os.pathsep.join([str(tmp_path), str(Path.cwd())]))

    old_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def local_lifespan(api_app):
        agent = ClioAgent(data_dir=str(tmp_path / "api-clio"), verbose=False)
        api_app.state.agent = agent
        api_app.state.healthy = True
        api_app.state.provider_config = LMProviderConfig(provider="ollama")
        try:
            yield
        finally:
            agent.shutdown()

    app.router.lifespan_context = local_lifespan
    try:
        with TestClient(app) as client:
            response = client.post(
                "/query",
                json={"question": f"What datasets are in {sample_hdf5}?"},
            )
    finally:
        app.router.lifespan_context = old_lifespan

    assert response.status_code == 200
    body = response.json()
    assert body["selected_expert"] == "data"
    assert "simulation/temperature" in body["answer"]
