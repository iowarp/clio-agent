"""Tests for the materials MCP server."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.materials_server import materials_server


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
def cif_file(tmp_path):
    path = tmp_path / "structure.cif"
    path.write_text(
        "data_SrTiO3_benchmark\n"
        "_chemical_formula_sum 'Sr1 Ti1 O3'\n"
        "_symmetry_space_group_name_H-M 'P m -3 m'\n"
        "_cell_length_a 3.905\n"
        "_cell_length_b 3.905\n"
        "_cell_length_c 3.905\n"
        "_cell_angle_alpha 90\n"
        "_cell_angle_beta 90\n"
        "_cell_angle_gamma 90\n"
        "loop_\n"
        "_atom_site_label\n"
        "_atom_site_type_symbol\n"
        "_atom_site_fract_x\n"
        "_atom_site_fract_y\n"
        "_atom_site_fract_z\n"
        "_atom_site_occupancy\n"
        "Sr1 Sr 0 0 0 1\n"
        "Ti1 Ti 0.5 0.5 0.5 1\n"
        "O1 O 0.5 0.5 0 1\n"
        "O2 O 0.5 0 0.5 1\n"
        "O3 O 0 0.5 0.5 1\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_inspect_cif_returns_structure_summary(cif_file):
    async with Client(materials_server) as client:
        result = await client.call_tool("inspect_cif", {"filepath": str(cif_file)})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["formula_sum"] == "Sr1 Ti1 O3"
    assert data["space_group"] == "P m -3 m"
    assert data["atom_site_count"] == 5
    assert data["species_counts"] == {"Sr": 1, "Ti": 1, "O": 3}
    assert data["cell_volume_angstrom3"] == pytest.approx(59.547, rel=1e-3)
    assert data["approx_density_g_cm3"] > 4.0


@pytest.mark.asyncio
async def test_gateway_exposes_materials_tool(cif_file):
    async with Client(gateway) as client:
        result = await client.call_tool("materials_inspect_cif", {"filepath": str(cif_file)})

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["species_counts"]["O"] == 3
