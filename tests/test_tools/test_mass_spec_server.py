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
async def test_gateway_exposes_mass_spec_tool(mzml_file):
    async with Client(gateway) as client:
        result = await client.call_tool("mass_spec_inspect_mzml", {"filepath": str(mzml_file)})

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["ms_levels"]["2"] == 1
