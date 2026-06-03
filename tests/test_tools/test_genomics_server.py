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


@pytest.fixture
def cohort_vcf_file(tmp_path):
    path = tmp_path / "cohort.vcf"
    path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tclean_A\tclean_B\tlow_call\texcess_het\n"
        "chr20\t1\t.\tA\tG\t60\tPASS\t.\tGT:DP\t0/0:22\t0/1:18\t./.:0\t0/1:25\n"
        "chr20\t2\t.\tC\tT\t60\tPASS\t.\tGT:DP\t0/1:20\t0/0:21\t./.:0\t0/1:24\n"
        "chr20\t3\t.\tG\tA\t60\tPASS\t.\tGT:DP\t1/1:19\t0/0:20\t./.:0\t0/1:23\n"
        "chr20\t4\t.\tT\tC\t60\tPASS\t.\tGT:DP\t0/0:20\t0/1:19\t0/0:12\t0/1:22\n"
        "chr20\t5\t.\tA\tC\t60\tPASS\t.\tGT:DP\t0/0:21\t1/1:20\t0/1:11\t0/1:21\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def no_sample_vcf_file(tmp_path):
    path = tmp_path / "no_samples.vcf"
    path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr20\t1\t.\tA\tG\t60\tPASS\t.\n",
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
async def test_vcf_cohort_qc_returns_per_sample_metrics(cohort_vcf_file):
    async with Client(genomics_server) as client:
        result = await client.call_tool(
            "vcf_cohort_qc",
            {
                "filepath": str(cohort_vcf_file),
                "low_call_rate_threshold": 0.8,
                "high_heterozygosity_z": 1.0,
            },
        )
    data = _parse_result(result)

    assert data["ok"] is True
    assert data["sample_count"] == 4
    assert data["variant_count"] == 5
    rows = {row["sample"]: row for row in data["samples"]}
    assert rows["clean_A"]["call_rate"] == 1.0
    assert rows["clean_A"]["genotype_counts"]["het"] == 1
    assert rows["low_call"]["call_rate"] == 0.4
    assert rows["low_call"]["missing"] == 3
    assert rows["excess_het"]["heterozygosity"] == 1.0

    flagged = {row["sample"]: row["reasons"] for row in data["flagged_samples"]}
    assert flagged["low_call"] == ["low_call_rate"]
    assert "high_heterozygosity" in flagged["excess_het"]


@pytest.mark.asyncio
async def test_vcf_cohort_qc_reports_no_sample_columns(no_sample_vcf_file):
    async with Client(genomics_server) as client:
        result = await client.call_tool(
            "vcf_cohort_qc",
            {"filepath": str(no_sample_vcf_file)},
        )
    data = _parse_result(result)

    assert data["ok"] is False
    assert data["sample_count"] == 0
    assert "No VCF sample columns" in data["error"]


@pytest.mark.asyncio
async def test_gateway_exposes_genomics_tools(fasta_file, vcf_file, cohort_vcf_file):
    async with Client(gateway) as client:
        fasta = await client.call_tool("genomics_inspect_fasta", {"filepath": str(fasta_file)})
        vcf = await client.call_tool("genomics_summarize_vcf", {"filepath": str(vcf_file)})
        cohort = await client.call_tool(
            "genomics_vcf_cohort_qc",
            {"filepath": str(cohort_vcf_file), "low_call_rate_threshold": 0.8},
        )

    assert _parse_result(fasta)["record_count"] == 2
    assert _parse_result(vcf)["variant_count"] == 2
    assert _parse_result(cohort)["sample_count"] == 4
