"""
Tests for HDF5 MCP Server

Uses in-memory Client(server) pattern for testing -- no subprocess, no network.
All tests use the sample_hdf5 fixture from conftest.py.
"""

import importlib
import json

import pytest
from fastmcp import Client

from clio_agent.tools.servers.hdf5_server import hdf5_server

hdf5_module = importlib.import_module("clio_agent.tools.servers.hdf5_server")


def _parse_result(result):
    """Extract dict from CallToolResult.

    FastMCP Client.call_tool returns a CallToolResult with .data attribute.
    For dict returns, .data is already a dict. For string returns, parse JSON.
    """
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}
    return {"raw": str(data)}


@pytest.mark.asyncio
async def test_file_policy_rejects_unsafe_hdf5_path_before_open(tmp_path, monkeypatch):
    """Unsafe paths should fail validation before h5py opens the file."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.h5"
    outside.write_bytes(b"not hdf5")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(allowed))

    def fail_open(*args, **kwargs):
        raise AssertionError("h5py.File should not be called")

    monkeypatch.setattr(hdf5_module.h5py, "File", fail_open)

    async with Client(hdf5_server) as client:
        result = await client.call_tool("analyze_file", {"filepath": str(outside)})
    data = _parse_result(result)

    assert data["error"]["type"] == "file_policy"
    assert data["error"]["code"] == "outside_allowed_roots"


@pytest.mark.asyncio
async def test_invalid_hdf5_args_rejected_before_open(sample_hdf5, monkeypatch):
    """Invalid access_pattern should fail before h5py opens the file."""

    def fail_open(*args, **kwargs):
        raise AssertionError("h5py.File should not be called")

    monkeypatch.setattr(hdf5_module.h5py, "File", fail_open)

    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "optimize_chunking",
            {
                "filepath": sample_hdf5,
                "dataset": "simulation/temperature",
                "access_pattern": "diagonal",
            },
        )
    data = _parse_result(result)

    assert data["error"]["type"] == "file_policy"
    assert data["error"]["code"] == "invalid_argument"
    assert data["error"]["field"] == "access_pattern"


@pytest.mark.asyncio
async def test_list_datasets(sample_hdf5):
    """Test listing all datasets in an HDF5 file."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("list_datasets", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_datasets"] == 3

        paths = [d["path"] for d in data["datasets"]]
        assert "simulation/temperature" in paths
        assert "simulation/pressure" in paths
        assert "timestamps" in paths


@pytest.mark.asyncio
async def test_list_datasets_shapes(sample_hdf5):
    """Test that dataset shapes are reported correctly."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("list_datasets", {"filepath": sample_hdf5})
        data = _parse_result(result)

        ds_by_path = {d["path"]: d for d in data["datasets"]}

        assert ds_by_path["simulation/temperature"]["shape"] == [100, 100]
        assert ds_by_path["simulation/temperature"]["dtype"] == "float64"
        assert ds_by_path["timestamps"]["shape"] == [1000]
        assert ds_by_path["timestamps"]["dtype"] == "int64"


@pytest.mark.asyncio
async def test_analyze_dataset(sample_hdf5):
    """Test analyzing a specific dataset."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "analyze_dataset",
            {"filepath": sample_hdf5, "dataset": "simulation/temperature"},
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["shape"] == [100, 100]
        assert data["dtype"] == "float64"
        assert data["compression"] == "gzip"
        assert data["compression_opts"] == 6
        assert data["chunks"] == [10, 10]
        assert data["is_chunked"] is True
        assert "statistics" in data
        assert "min" in data["statistics"]
        assert "max" in data["statistics"]
        assert "mean" in data["statistics"]


@pytest.mark.asyncio
async def test_analyze_dataset_attributes(sample_hdf5):
    """Test that dataset attributes are included in analysis."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "analyze_dataset",
            {"filepath": sample_hdf5, "dataset": "simulation/temperature"},
        )
        data = _parse_result(result)

        assert "attributes" in data
        assert data["attributes"]["units"] == "Kelvin"
        assert data["attributes"]["description"] == "Surface temperature"


@pytest.mark.asyncio
async def test_analyze_dataset_contiguous(sample_hdf5):
    """Test analyzing a contiguous (non-chunked) dataset."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "analyze_dataset",
            {"filepath": sample_hdf5, "dataset": "timestamps"},
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["is_chunked"] is False
        assert data["chunks"] is None
        assert data["compression"] is None


@pytest.mark.asyncio
async def test_check_compression(sample_hdf5):
    """Test compression check across all datasets."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("check_compression", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_datasets"] == 3
        # Only temperature has gzip compression
        assert data["compressed_datasets"] == 1
        assert data["uncompressed_datasets"] == 2

        ds_by_path = {d["path"]: d for d in data["datasets"]}
        assert ds_by_path["simulation/temperature"]["compression"] == "gzip"
        assert ds_by_path["simulation/pressure"]["compression"] is None
        assert ds_by_path["timestamps"]["compression"] is None


@pytest.mark.asyncio
async def test_check_compression_ratios(sample_hdf5):
    """Test that compression ratios are calculated."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("check_compression", {"filepath": sample_hdf5})
        data = _parse_result(result)

        ds_by_path = {d["path"]: d for d in data["datasets"]}
        temp = ds_by_path["simulation/temperature"]
        # Compressed data should have a ratio > 1.0 (smaller on disk)
        assert temp["compression_ratio"] is not None
        assert temp["compression_ratio"] > 0


@pytest.mark.asyncio
async def test_optimize_chunking_row(sample_hdf5):
    """Test chunk optimization for row access pattern."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "optimize_chunking",
            {
                "filepath": sample_hdf5,
                "dataset": "simulation/temperature",
                "access_pattern": "row",
            },
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["access_pattern"] == "row"
        assert "recommended_chunks" in data
        assert len(data["recommended_chunks"]) == 2
        assert data["is_currently_chunked"] is True
        assert data["current_chunks"] == [10, 10]


@pytest.mark.asyncio
async def test_optimize_chunking_contiguous(sample_hdf5):
    """Test chunk optimization for a contiguous dataset."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "optimize_chunking",
            {
                "filepath": sample_hdf5,
                "dataset": "timestamps",
                "access_pattern": "row",
            },
        )
        data = _parse_result(result)

        assert "error" not in data
        assert data["is_currently_chunked"] is False
        assert "contiguous" in data["rationale"].lower() or "not chunked" in data["rationale"].lower()


@pytest.mark.asyncio
async def test_optimize_chunking_invalid_pattern(sample_hdf5):
    """Test chunk optimization with invalid access pattern."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "optimize_chunking",
            {
                "filepath": sample_hdf5,
                "dataset": "simulation/temperature",
                "access_pattern": "diagonal",
            },
        )
        data = _parse_result(result)
        assert "error" in data


@pytest.mark.asyncio
async def test_optimize_chunking_synonym_normalization(sample_hdf5):
    """Natural-language access-pattern synonyms map to the canonical enum."""
    async with Client(hdf5_server) as client:
        for synonym in ("row-wise", "sequential", "time-series", "Columnar", "balanced"):
            result = await client.call_tool(
                "optimize_chunking",
                {
                    "filepath": sample_hdf5,
                    "dataset": "simulation/temperature",
                    "access_pattern": synonym,
                },
            )
            data = _parse_result(result)
            assert "error" not in data, f"{synonym!r} should normalize, got {data}"


@pytest.mark.asyncio
async def test_analyze_file(sample_hdf5):
    """Test high-level file analysis."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("analyze_file", {"filepath": sample_hdf5})
        data = _parse_result(result)

        assert "error" not in data
        assert data["total_datasets"] == 3
        assert data["total_groups"] == 1  # /simulation
        assert "simulation" in data["groups"]
        assert data["root_attributes"]["created_by"] == "clio-agent-test"
        assert data["root_attributes"]["version"] == "1.0"
        assert "compression_summary" in data
        assert data["compression_summary"]["compressed_datasets"] == 1


@pytest.mark.asyncio
async def test_invalid_filepath():
    """Test that invalid file path returns error, not crash."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool("analyze_file", {"filepath": "/nonexistent/file.h5"})
        data = _parse_result(result)
        assert "error" in data


@pytest.mark.asyncio
async def test_invalid_dataset_path(sample_hdf5):
    """Test that invalid dataset path returns error."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "analyze_dataset",
            {"filepath": sample_hdf5, "dataset": "nonexistent/dataset"},
        )
        data = _parse_result(result)
        assert "error" in data


@pytest.mark.asyncio
async def test_tool_inventory():
    """Server exposes the DataExpert tool set plus the HDF5Expert tool set."""
    async with Client(hdf5_server) as client:
        tools = await client.list_tools()
        tool_names = sorted([t.name for t in tools])
        assert tool_names == [
            "analyze_dataset",
            "analyze_file",
            "apply_filter",
            "check_cf_compliance",
            "check_compression",
            "consult_skill",
            "get_object_metadata",
            "list_datasets",
            "optimize_chunking",
            "rechunk_dataset",
            "visualize_dataset",
        ]


# ============================================================================
# HDF5Expert tools
# ============================================================================


import shutil  # noqa: E402

import numpy as np  # noqa: E402


@pytest.mark.asyncio
async def test_get_object_metadata_dataset(sample_hdf5):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "get_object_metadata",
            {"filepath": sample_hdf5, "object_path": "/simulation/temperature"},
        )
    data = _parse_result(result)
    assert "error" not in data
    assert data["object_type"] == "dataset"
    assert data["shape"] == [100, 100]
    assert data["is_chunked"] is True
    assert data["chunks"] == [10, 10]
    assert data["compression"] == "gzip"
    assert "attributes" in data
    assert data["attributes"]["units"]["value"] == "Kelvin"
    assert "statistics" in data


@pytest.mark.asyncio
async def test_get_object_metadata_group(sample_hdf5):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "get_object_metadata",
            {"filepath": sample_hdf5, "object_path": "/simulation"},
        )
    data = _parse_result(result)
    assert data["object_type"] == "group"
    assert set(data["members"]) == {"temperature", "pressure"}


@pytest.mark.asyncio
async def test_get_object_metadata_missing(sample_hdf5):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "get_object_metadata",
            {"filepath": sample_hdf5, "object_path": "/nope"},
        )
    data = _parse_result(result)
    assert data["error"]["code"] == "object_not_found"


@pytest.mark.asyncio
async def test_rechunk_dataset_requires_exactly_one_layout_arg(sample_hdf5):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "rechunk_dataset",
            {"filepath": sample_hdf5, "object_path": "/simulation/temperature"},
        )
    data = _parse_result(result)
    assert data["error"]["code"] == "ambiguous_layout_request"


@pytest.mark.asyncio
async def test_rechunk_dataset_chunk_dims(sample_hdf5, tmp_path):
    if shutil.which("h5repack") is None:
        pytest.skip("h5repack not installed")
    out = tmp_path / "rechunked.h5"
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "rechunk_dataset",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/temperature",
                "chunk_dims": "50x50",
                "output_filepath": str(out),
            },
        )
    data = _parse_result(result)
    assert data.get("success") is True, data
    assert data["new_chunks"] == [50, 50]
    assert out.exists()


@pytest.mark.asyncio
async def test_rechunk_dataset_adjustment(sample_hdf5, tmp_path):
    if shutil.which("h5repack") is None:
        pytest.skip("h5repack not installed")
    out = tmp_path / "doubled.h5"
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "rechunk_dataset",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/temperature",
                "chunk_adjustment": "double",
                "output_filepath": str(out),
            },
        )
    data = _parse_result(result)
    assert data.get("success") is True, data
    assert data["original_chunks"] == [10, 10]
    assert data["new_chunks"] == [20, 20]


@pytest.mark.asyncio
async def test_rechunk_dataset_h5repack_missing(sample_hdf5, monkeypatch, tmp_path):
    monkeypatch.setattr(hdf5_module.shutil, "which", lambda _name: None)
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "rechunk_dataset",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/temperature",
                "chunk_dims": "50x50",
                "output_filepath": str(tmp_path / "x.h5"),
            },
        )
    data = _parse_result(result)
    assert data["error"]["code"] == "h5repack_not_found"


@pytest.mark.asyncio
async def test_apply_filter_gzip(sample_hdf5, tmp_path):
    if shutil.which("h5repack") is None:
        pytest.skip("h5repack not installed")
    out = tmp_path / "filtered.h5"
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "apply_filter",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/pressure",
                "filter_type": "gzip",
                "compression_level": 4,
                "output_filepath": str(out),
            },
        )
    data = _parse_result(result)
    assert data.get("success") is True, data
    assert data["new_filters"]["compression"] == "gzip"
    assert data["new_filters"]["compression_opts"] == 4


@pytest.mark.asyncio
async def test_apply_filter_requires_choice(sample_hdf5, tmp_path):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "apply_filter",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/pressure",
                "output_filepath": str(tmp_path / "x.h5"),
            },
        )
    data = _parse_result(result)
    assert data["error"]["code"] == "no_filter_specified"


@pytest.mark.asyncio
async def test_visualize_dataset_1d(sample_hdf5, tmp_path):
    out = tmp_path / "ts.png"
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "visualize_dataset",
            {
                "filepath": sample_hdf5,
                "object_path": "/timestamps",
                "save_path": str(out),
            },
        )
    data = _parse_result(result)
    assert data.get("success") is True, data
    assert data["plot_type"] == "line"
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_visualize_dataset_2d(sample_hdf5, tmp_path):
    out = tmp_path / "temp.png"
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "visualize_dataset",
            {
                "filepath": sample_hdf5,
                "object_path": "/simulation/temperature",
                "save_path": str(out),
            },
        )
    data = _parse_result(result)
    assert data.get("success") is True, data
    assert data["plot_type"] == "imshow"
    assert out.exists()


@pytest.mark.asyncio
async def test_visualize_dataset_rejects_3d(tmp_path):
    """visualize_dataset only handles rank-1 and rank-2 numeric datasets."""
    fpath = tmp_path / "cube.h5"
    with hdf5_module.h5py.File(fpath, "w") as f:
        f.create_dataset("/cube", data=np.zeros((4, 4, 4)))
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "visualize_dataset",
            {"filepath": str(fpath), "object_path": "/cube"},
        )
    data = _parse_result(result)
    assert data["error"]["code"] == "unsupported_rank"


@pytest.mark.asyncio
async def test_check_cf_compliance_plain_hdf5(sample_hdf5):
    """Plain h5py-written files have no CF/NetCDF markers — flagged high."""
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "check_cf_compliance", {"filepath": sample_hdf5}
        )
    data = _parse_result(result)
    assert data["status"] == "ok"
    assert data["file_format"] == "HDF5"
    assert data["declared_conventions"] is None
    assert data["issue_counts"]["high"] >= 1
    assert any(i["path"] == "/" for i in data["issues"])


@pytest.mark.asyncio
async def test_consult_skill_exact_name(sample_hdf5):
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "consult_skill", {"topic": "hdf5-chunking"}
        )
    data = _parse_result(result)
    assert "error" not in data
    assert data["skill_name"] == "hdf5-chunking"
    assert data["matched_by"] == "exact_name"
    assert "Chunk" in data["body"] or "chunk" in data["body"]
    assert data["alternatives"] == []


@pytest.mark.asyncio
async def test_consult_skill_topic_match():
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "consult_skill",
            {"topic": "I want to use ros3 to read HDF5 from S3"},
        )
    data = _parse_result(result)
    assert "error" not in data
    assert data["skill_name"] == "hdf5-ros3-vfd"
    assert data["matched_by"] == "topic_match"
    assert data["body"]
    assert isinstance(data["alternatives"], list)


@pytest.mark.asyncio
async def test_consult_skill_no_match():
    async with Client(hdf5_server) as client:
        result = await client.call_tool(
            "consult_skill", {"topic": "the weather today is nice"}
        )
    data = _parse_result(result)
    assert "error" in data
    assert data["error"]["code"] == "no_skill_match"


@pytest.mark.asyncio
async def test_consult_skill_rejects_empty_topic():
    async with Client(hdf5_server) as client:
        result = await client.call_tool("consult_skill", {"topic": "   "})
    data = _parse_result(result)
    assert data["error"]["code"] == "empty_topic"


@pytest.mark.asyncio
async def test_check_cf_compliance_cf_like_file(tmp_path):
    """A file with CF-shaped metadata scores 100%."""
    fpath = tmp_path / "cf.h5"
    with hdf5_module.h5py.File(fpath, "w") as f:
        f.attrs["Conventions"] = "CF-1.11"
        f.attrs["_NCProperties"] = "version=2"
        ds = f.create_dataset("/temperature", data=np.zeros(10, dtype="f8"))
        ds.attrs["units"] = "K"
        ds.attrs["standard_name"] = "air_temperature"
        ds.attrs["long_name"] = "Air temperature"
    async with Client(hdf5_server) as client:
        result = await client.call_tool("check_cf_compliance", {"filepath": str(fpath)})
    data = _parse_result(result)
    assert data["status"] == "ok"
    assert data["file_format"] == "NetCDF4"
    assert data["declared_conventions"] == "CF-1.11"
    assert data["score_percent"] == 100.0
    assert data["issue_counts"]["high"] == 0
