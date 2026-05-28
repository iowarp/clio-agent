"""Tests for the genomics MCP server."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.genomics_server import genomics_server


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
def fasta_file(tmp_path):
    path = tmp_path / "reference.fasta"
    path.write_text(
        ">chrA synthetic reference\n"
        "ACGTACGTNN\n"
        ">plasmidB auxiliary replicon\n"
        "GGGGCCCCAAAA\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def vcf_file(tmp_path):
    path = tmp_path / "variants.vcf"
    path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample_A\n"
        "chrA\t5\tvar001\tA\tG\t72.0\tPASS\tGENE=repA;EFFECT=missense\tGT:DP\t0/1:42\n"
        "plasmidB\t8\tvar002\tCT\tC\t54.5\tPASS\tGENE=mobility;EFFECT=frameshift\tGT:DP\t0/1:36\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_inspect_fasta_returns_sequence_summary(fasta_file):
    async with Client(genomics_server) as client:
        result = await client.call_tool("inspect_fasta", {"filepath": str(fasta_file)})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["record_count"] == 2
    assert data["total_bases"] == 22
    assert data["longest_record"]["id"] == "plasmidB"
    assert data["base_counts"]["N"] == 2


@pytest.mark.asyncio
async def test_summarize_vcf_returns_variant_counts(vcf_file):
    async with Client(genomics_server) as client:
        result = await client.call_tool("summarize_vcf", {"filepath": str(vcf_file)})
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["sample_count"] == 1
    assert data["variant_count"] == 2
    assert data["variant_types"]["snp"] == 1
    assert data["variant_types"]["deletion"] == 1
    assert data["effects"]["missense"] == 1


@pytest.mark.asyncio
async def test_gateway_exposes_genomics_tools(fasta_file, vcf_file):
    async with Client(gateway) as client:
        fasta = await client.call_tool("genomics_inspect_fasta", {"filepath": str(fasta_file)})
        vcf = await client.call_tool("genomics_summarize_vcf", {"filepath": str(vcf_file)})

    assert _parse_result(fasta)["record_count"] == 2
    assert _parse_result(vcf)["variant_count"] == 2
