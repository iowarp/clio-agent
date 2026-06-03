"""Tests for the mass spectrometry MCP server."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.mass_spec_server import mass_spec_server


def _parse_result(result):
    """Extract a dict from FastMCP CallToolResult."""
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}
    return {"raw": str(data)}


@pytest.fixture
def mzml_file(tmp_path):
    path = tmp_path / "sample.mzML"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<mzML xmlns="http://psi.hupo.org/ms/mzml">
  <run>
    <spectrumList count="2">
      <spectrum id="scan=1" defaultArrayLength="3">
        <cvParam name="ms level" value="1"/>
        <cvParam name="total ion current" value="600.0"/>
        <scanList><scan><cvParam name="scan start time" value="0.10"/></scan></scanList>
        <binaryDataArrayList>
          <binaryDataArray><cvParam name="m/z array"/><binary>100.0 200.0 300.0</binary></binaryDataArray>
          <binaryDataArray><cvParam name="intensity array"/><binary>100.0 200.0 300.0</binary></binaryDataArray>
        </binaryDataArrayList>
      </spectrum>
      <spectrum id="scan=2" defaultArrayLength="2">
        <cvParam name="ms level" value="2"/>
        <cvParam name="total ion current" value="125.0"/>
        <binaryDataArrayList>
          <binaryDataArray><cvParam name="m/z array"/><binary>150.0 250.0</binary></binaryDataArray>
          <binaryDataArray><cvParam name="intensity array"/><binary>75.0 50.0</binary></binaryDataArray>
        </binaryDataArrayList>
      </spectrum>
    </spectrumList>
  </run>
</mzML>
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lfq_table(tmp_path):
    path = tmp_path / "proteinGroups.txt"
    path.write_text(
        "Protein IDs\tGene names\tReverse\tPotential contaminant\tIntensity 6A_1\tIntensity 6A_2\tIntensity 6A_3\tIntensity 6B_1\tIntensity 6B_2\tIntensity 6B_3\n"
        "UPS1_P001\tUPS1A\t\t\t100\t120\t110\t1125\t1100\t1150\n"
        "UPS1_P002\tUPS1B\t\t\t95\t105\t100\t1012\t1037\t1000\n"
        "YEAST_P001\tACT1\t\t\t1000\t1010\t990\t4200\t4000\t4100\n"
        "YEAST_P002\tTUB1\t\t\t500\t520\t510\t2050\t2100\t2080\n"
        "YEAST_P003\tRPL3\t\t\t750\t760\t740\t3000\t\t3050\n"
        "CON__TRYPSIN\tTRY\t\t+\t100\t100\t100\t100\t100\t100\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_inspect_mzml_returns_spectrum_summary(mzml_file):
    async with Client(mass_spec_server) as client:
        result = await client.call_tool("inspect_mzml", {"filepath": str(mzml_file)})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["format"] == "mzML"
    assert data["spectrum_count"] == 2
    assert data["ms_levels"] == {"1": 1, "2": 1}
    assert data["total_peak_count"] == 5
    assert data["mz_range"] == [100.0, 300.0]
    assert data["tic_total"] == 725.0
    assert data["total_ion_current_total"] == 725.0
    assert data["total_ion_current_max"] == 600.0
    assert data["representative_spectra"][0]["id"] == "scan=1"


@pytest.mark.asyncio
async def test_lfq_differential_abundance_selects_median_normalization(lfq_table):
    async with Client(mass_spec_server) as client:
        result = await client.call_tool(
            "lfq_differential_abundance",
            {
                "filepath": str(lfq_table),
                "group_a_prefix": "Intensity 6A_",
                "group_b_prefix": "Intensity 6B_",
                "spike_terms": "UPS1",
                "expected_spike_log2fc": 1.566,
                "min_observed_per_group": 2,
            },
        )
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["removed_contaminant_or_reverse_rows"] == 1
    assert data["selected_normalization"] == "median"
    assert data["normalization_methods"]["raw"]["spike_abs_error"] > 0.3
    assert data["normalization_methods"]["median"]["spike_abs_error"] < 0.3
    assert data["sample_missingness"]["Intensity 6B_2"]["missing"] == 1
    ranked = {row["protein"]: row for row in data["ranked_proteins"]}
    assert ranked["UPS1_P001"]["log2_fold_change"] > 1.3
    assert ranked["UPS1_P002"]["log2_fold_change"] > 1.3
    assert "CON__TRYPSIN" not in ranked


@pytest.mark.asyncio
async def test_lfq_differential_abundance_reports_missing_groups(lfq_table):
    async with Client(mass_spec_server) as client:
        result = await client.call_tool(
            "lfq_differential_abundance",
            {
                "filepath": str(lfq_table),
                "group_a_prefix": "missing_A",
                "group_b_prefix": "Intensity 6B_",
            },
        )
    data = _parse_result(result)

    assert data["ok"] is False
    assert "Could not find intensity columns" in data["error"]
    assert "Intensity 6B_1" in data["available_numeric_columns"]


@pytest.mark.asyncio
async def test_gateway_exposes_mass_spec_tool(mzml_file, lfq_table):
    async with Client(gateway) as client:
        result = await client.call_tool("mass_spec_inspect_mzml", {"filepath": str(mzml_file)})
        lfq = await client.call_tool(
            "mass_spec_lfq_differential_abundance",
            {
                "filepath": str(lfq_table),
                "group_a_prefix": "Intensity 6A_",
                "group_b_prefix": "Intensity 6B_",
                "spike_terms": "UPS1",
                "expected_spike_log2fc": 1.566,
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["ms_levels"]["2"] == 1
    assert _parse_result(lfq)["selected_normalization"] == "median"
