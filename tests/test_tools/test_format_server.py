"""Tests for scientific format bridge tools."""

from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pyarrow.parquet as pq
import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.format_server import convert_hdf5_to_parquet, format_server


def _parse_result(result):
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
def edge_hdf5(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    path = tmp_path / "edge_cases.h5"
    with h5py.File(path, "w") as handle:
        float_col = np.linspace(0, 1, 32, dtype=np.float64)
        float_col[13] = np.nan
        handle.create_dataset("float_with_nan", data=float_col, compression="gzip")
        handle.create_dataset("labels", data=np.array(["alpha", "beta"] * 16, dtype=h5py.string_dtype()))
        handle.create_dataset(
            "uint64_overflow",
            data=np.array([np.iinfo(np.int64).max + 1, np.iinfo(np.int64).max + 2], dtype=np.uint64),
        )
        handle.create_dataset("float16_lossy", data=np.linspace(0, 1, 32, dtype=np.float16))
        handle.create_dataset("complex_signal", data=np.arange(32, dtype=np.float64) + 1j)
        datetime = handle.create_dataset("time", data=np.arange(32, dtype=np.int64))
        datetime.attrs["logical_type"] = "datetime64[ns]"
    return path


def test_convert_hdf5_to_parquet_flags_dtype_risks(edge_hdf5: Path, tmp_path: Path) -> None:
    output = tmp_path / "converted.parquet"

    result = convert_hdf5_to_parquet(str(edge_hdf5), str(output))

    assert result["ok"] is True
    assert output.exists()
    assert result["row_count_match"] is True
    assert result["output_row_count"] == 32
    converted = {entry["dataset"]: entry for entry in result["converted_columns"]}
    skipped = {entry["dataset"]: entry for entry in result["skipped_columns"]}
    assert converted["float_with_nan"]["source_nan_count"] == 1
    assert converted["float_with_nan"]["output_nan_count"] == 1
    assert converted["float_with_nan"]["checksum_match"] is True
    assert converted["labels"]["checksum_match"] is True
    assert skipped["float16_lossy"]["lossy"] is True
    assert skipped["complex_signal"]["lossy"] is True
    assert skipped["uint64_overflow"]["unsafe"] is True
    assert skipped["time"]["reason"] == "datetime_logical_type_requires_explicit_policy"
    table = pq.read_table(output)
    assert table.num_rows == 32
    assert set(table.column_names) == {"float_with_nan", "labels"}


def test_convert_hdf5_to_parquet_denies_out_of_root_without_write(
    edge_hdf5: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path.parent / "outside.parquet"
    if output.exists():
        output.unlink()

    result = convert_hdf5_to_parquet(str(edge_hdf5), str(output))

    assert result["error"]["code"] == "outside_allowed_roots"
    assert not output.exists()


@pytest.mark.asyncio
async def test_format_gateway_exposes_convert_tool(edge_hdf5: Path, tmp_path: Path) -> None:
    direct_output = tmp_path / "direct.parquet"
    gateway_output = tmp_path / "gateway.parquet"
    async with Client(format_server) as client:
        direct = await client.call_tool(
            "convert_hdf5_to_parquet",
            {"filepath": str(edge_hdf5), "output_path": str(direct_output)},
        )
    async with Client(gateway) as client:
        via_gateway = await client.call_tool(
            "format_convert_hdf5_to_parquet",
            {"filepath": str(edge_hdf5), "output_path": str(gateway_output)},
        )

    assert _parse_result(direct)["ok"] is True
    assert _parse_result(via_gateway)["ok"] is True
