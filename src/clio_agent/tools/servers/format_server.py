"""Scientific format conversion tools for CLIO."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path, validate_write_path

format_server = FastMCP("format")

_INT64_MAX = np.iinfo(np.int64).max


def _dataset_logical_type(dataset: h5py.Dataset) -> str:
    value = dataset.attrs.get("logical_type") or dataset.attrs.get("semantic_type") or ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _column_name(path: str) -> str:
    return path.strip("/").replace("/", "__")


def _collect_1d_datasets(group: h5py.Group, prefix: str = "") -> list[tuple[str, h5py.Dataset]]:
    datasets: list[tuple[str, h5py.Dataset]] = []
    for name, item in group.items():
        path = f"{prefix}/{name}" if prefix else name
        if isinstance(item, h5py.Dataset):
            if len(item.shape) == 1:
                datasets.append((path, item))
        elif isinstance(item, h5py.Group):
            datasets.extend(_collect_1d_datasets(item, path))
    return datasets


def _nan_count(array: np.ndarray) -> int:
    if not np.issubdtype(array.dtype, np.floating):
        return 0
    return int(np.isnan(array).sum())


def _column_checksum(values: list[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        if isinstance(value, float) and math.isnan(value):
            token = b"<NaN>"
        else:
            token = repr(value).encode("utf-8", errors="replace")
        digest.update(token)
        digest.update(b"\0")
    return digest.hexdigest()


def _to_python_values(array: np.ndarray) -> list[Any]:
    values = array.tolist()
    if not isinstance(values, list):
        return [values]
    out: list[Any] = []
    for value in values:
        if isinstance(value, bytes):
            out.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, np.generic):
            out.append(value.item())
        else:
            out.append(value)
    return out


def _dtype_decision(dataset: h5py.Dataset, array: np.ndarray) -> dict[str, Any]:
    dtype = array.dtype
    logical_type = _dataset_logical_type(dataset).lower()
    if "datetime" in logical_type:
        return {
            "status": "skipped",
            "reason": "datetime_logical_type_requires_explicit_policy",
            "lossy": True,
            "unsafe": False,
        }
    if np.issubdtype(dtype, np.complexfloating):
        return {
            "status": "skipped",
            "reason": "complex_dtype_has_no_single_faithful_parquet_scalar",
            "lossy": True,
            "unsafe": False,
        }
    if dtype == np.float16:
        return {
            "status": "skipped",
            "reason": "float16_requires_lossy_widening_policy",
            "lossy": True,
            "unsafe": False,
        }
    if np.issubdtype(dtype, np.unsignedinteger) and dtype.itemsize >= 8:
        max_value = int(np.max(array)) if array.size else 0
        if max_value > _INT64_MAX:
            return {
                "status": "skipped",
                "reason": "uint64_value_overflows_int64_compatibility_policy",
                "lossy": False,
                "unsafe": True,
                "max_value": max_value,
            }
    if dtype.kind in {"O", "S", "U", "b", "i", "u", "f"}:
        return {"status": "converted", "reason": "dtype_supported", "lossy": False, "unsafe": False}
    return {
        "status": "skipped",
        "reason": f"unsupported_dtype_{dtype}",
        "lossy": True,
        "unsafe": True,
    }


def _arrow_array(array: np.ndarray) -> pa.Array:
    if array.dtype.kind in {"S", "U", "O"}:
        return pa.array(_to_python_values(array), type=pa.string())
    return pa.array(array)


def _parquet_column_summary(table: pa.Table) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for name in table.column_names:
        values = table[name].to_pylist()
        null_count = sum(value is None for value in values)
        nan_count = sum(isinstance(value, float) and math.isnan(value) for value in values)
        summary[name] = {
            "row_count": len(values),
            "null_count": null_count,
            "nan_count": nan_count,
            "checksum": _column_checksum(values),
            "type": str(table.schema.field(name).type),
        }
    return summary


@format_server.tool()
def convert_hdf5_to_parquet(
    filepath: str,
    output_path: str,
    skip_unsafe: bool = True,
    max_columns: int = 256,
) -> dict[str, Any]:
    """Convert compatible 1-D HDF5 datasets to Parquet with dtype integrity evidence.

    Use this for tabular HDF5-to-Parquet bridge requests where preserving
    values matters more than forcing every column through. The tool refuses
    writes outside CLIO's allowed roots, skips unsafe/lossy columns by default,
    and returns an integrity report for converted and skipped columns.
    """
    try:
        safe_input = validate_read_path(filepath)
        safe_output = validate_write_path(output_path)
        if safe_output.exists() and safe_output.is_dir():
            raise FilePolicyError(
                code="not_a_file",
                message=f"Output path is a directory: {safe_output}",
                field="output_path",
                path=str(safe_output),
                next_action="Provide a Parquet file path, not a directory.",
            )
        max_columns = max(1, min(int(max_columns or 256), 2048))
        columns: list[pa.Array] = []
        names: list[str] = []
        decisions: list[dict[str, Any]] = []
        row_count: int | None = None
        with h5py.File(safe_input, "r") as h5:
            datasets = _collect_1d_datasets(h5)[:max_columns]
            for dataset_path, dataset in datasets:
                array = np.asarray(dataset[()])
                decision = _dtype_decision(dataset, array)
                column_name = _column_name(dataset_path)
                entry: dict[str, Any] = {
                    "dataset": dataset_path,
                    "column": column_name,
                    "source_dtype": str(array.dtype),
                    "shape": list(array.shape),
                    "source_nan_count": _nan_count(array),
                    **decision,
                }
                if row_count is None:
                    row_count = int(array.shape[0])
                elif int(array.shape[0]) != row_count:
                    entry.update(
                        {
                            "status": "skipped",
                            "reason": "row_count_mismatch",
                            "lossy": False,
                            "unsafe": True,
                            "expected_rows": row_count,
                            "actual_rows": int(array.shape[0]),
                        }
                    )
                if entry["status"] == "converted":
                    values = _to_python_values(array)
                    entry["source_checksum"] = _column_checksum(values)
                    columns.append(_arrow_array(array))
                    names.append(column_name)
                elif not skip_unsafe:
                    return {
                        "ok": False,
                        "filepath": str(safe_input),
                        "output_path": str(safe_output),
                        "error": "Unsafe or lossy column requires explicit conversion policy",
                        "blocked_column": entry,
                    }
                decisions.append(entry)

        if not columns:
            return {
                "ok": False,
                "filepath": str(safe_input),
                "output_path": str(safe_output),
                "error": "No compatible 1-D HDF5 datasets were available for conversion",
                "columns": decisions,
            }
        table = pa.Table.from_arrays(columns, names=names)
        safe_output.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, safe_output)
        parquet_file = pq.ParquetFile(safe_output)
        output_table = parquet_file.read()
        output_summary = _parquet_column_summary(output_table)
        for entry in decisions:
            if entry["status"] == "converted":
                out = output_summary.get(str(entry["column"]), {})
                entry["output_type"] = out.get("type")
                entry["output_nan_count"] = out.get("nan_count")
                entry["output_checksum"] = out.get("checksum")
                entry["checksum_match"] = entry.get("source_checksum") == out.get("checksum")
                entry["nan_count_match"] = entry.get("source_nan_count") == out.get("nan_count")
        converted = [entry for entry in decisions if entry["status"] == "converted"]
        skipped = [entry for entry in decisions if entry["status"] != "converted"]
        return {
            "ok": True,
            "filepath": str(safe_input),
            "output_path": str(safe_output),
            "input_row_count": row_count or 0,
            "output_row_count": parquet_file.metadata.num_rows,
            "row_count_match": (row_count or 0) == parquet_file.metadata.num_rows,
            "converted_column_count": len(converted),
            "skipped_column_count": len(skipped),
            "converted_columns": converted,
            "skipped_columns": skipped,
            "integrity": {
                "all_converted_checksums_match": all(
                    bool(entry.get("checksum_match")) for entry in converted
                ),
                "all_converted_nan_counts_match": all(
                    bool(entry.get("nan_count_match")) for entry in converted
                ),
                "unsafe_or_lossy_columns_flagged": [
                    entry
                    for entry in skipped
                    if bool(entry.get("unsafe")) or bool(entry.get("lossy"))
                ],
            },
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


__all__ = ["format_server"]
