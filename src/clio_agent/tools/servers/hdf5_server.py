"""
HDF5 MCP Server for CLIO Agent

A FastMCP server providing HDF5 inspection, optimization, visualization,
and standards-checking tools using real h5py operations. Each tool is
designed for a specific agent use case in the CLIO data analysis workflow.

Two curated sets live on this server:

Original 5 (used by DataExpert):
    - list_datasets, analyze_dataset, check_compression,
      optimize_chunking, analyze_file

Added for HDF5Expert:
    - get_object_metadata    Richer per-object inspection (datasets, groups,
                             datatypes, links).
    - rechunk_dataset        h5repack-backed chunk-layout change.
                             Always writes a new file.
    - apply_filter           h5repack-backed filter/compression change.
                             Always writes a new file.
    - visualize_dataset      Matplotlib PNG plot from a 1D/2D dataset.
    - check_cf_compliance    Lightweight CF-conventions check on the
                             root + dataset attributes (no external deps).

Per-expert curation is enforced at the expert level via an explicit
tool allowlist, not by this server.

Usage:
    >>> from clio_agent.tools.servers.hdf5_server import hdf5_server
    >>> # Use with FastMCP Client for in-memory testing
    >>> from fastmcp import Client
    >>> async with Client(hdf5_server) as client:
    ...     result = await client.call_tool("analyze_file", {"filepath": "data.h5"})
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

import h5py
import numpy as np
from fastmcp import FastMCP

from clio_agent.tools.file_policy import (
    FilePolicyError,
    validate_choice,
    validate_non_empty_string,
    validate_read_path,
    validate_write_path,
)

hdf5_server = FastMCP("hdf5")


def _collect_datasets(group: h5py.Group, prefix: str = "") -> list[dict[str, Any]]:
    """Recursively collect all datasets from an HDF5 group.

    Args:
        group: HDF5 group to traverse
        prefix: Current path prefix for nested groups

    Returns:
        List of dataset info dicts with path, shape, dtype, size_bytes
    """
    datasets = []
    for name, item in group.items():
        path = f"{prefix}/{name}" if prefix else name
        if isinstance(item, h5py.Dataset):
            datasets.append({
                "path": path,
                "shape": list(item.shape),
                "dtype": str(item.dtype),
                "size_bytes": int(item.nbytes),
            })
        elif isinstance(item, h5py.Group):
            datasets.extend(_collect_datasets(item, path))
    return datasets


def _collect_groups(group: h5py.Group, prefix: str = "") -> list[str]:
    """Recursively collect all group paths from an HDF5 group.

    Args:
        group: HDF5 group to traverse
        prefix: Current path prefix

    Returns:
        List of group path strings
    """
    groups = []
    for name, item in group.items():
        path = f"{prefix}/{name}" if prefix else name
        if isinstance(item, h5py.Group):
            groups.append(path)
            groups.extend(_collect_groups(item, path))
    return groups


_ACCESS_PATTERN_SYNONYMS = {
    "row": "row",
    "row-wise": "row",
    "row_wise": "row",
    "rowwise": "row",
    "sequential": "row",
    "time-series": "row",
    "time_series": "row",
    "timeseries": "row",
    "column": "column",
    "col": "column",
    "column-wise": "column",
    "column_wise": "column",
    "columnwise": "column",
    "columnar": "column",
    "random": "random",
    "balanced": "random",
    "mixed": "random",
}


def _normalize_access_pattern(value: str) -> str:
    """Map natural-language access-pattern phrases to the canonical enum."""
    if not isinstance(value, str):
        return value
    key = value.strip().lower().split()[0] if value.strip() else value
    return _ACCESS_PATTERN_SYNONYMS.get(key, value)


@hdf5_server.tool()
def list_datasets(filepath: str) -> dict[str, Any]:
    """List all datasets in an HDF5 file with their shapes, dtypes, and sizes.

    Agent story: When a user asks what's in their HDF5 file, or when I need
    to discover available data before analysis.

    Args:
        filepath: Path to the HDF5 file

    Returns:
        Dictionary with 'datasets' list containing path, shape, dtype, size_bytes
        for each dataset, plus 'total_datasets' count.
    """
    try:
        safe_path = validate_read_path(filepath)
        with h5py.File(safe_path, "r") as f:
            datasets = _collect_datasets(f)
            return {
                "filepath": str(safe_path),
                "total_datasets": len(datasets),
                "datasets": datasets,
            }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@hdf5_server.tool()
def analyze_dataset(filepath: str, dataset: str) -> dict[str, Any]:
    """Analyze a specific dataset: shape, dtype, compression, chunk shape, statistics.

    Agent story: When a user asks about a specific dataset's properties or I need
    statistics for optimization recommendations.

    Args:
        filepath: Path to the HDF5 file
        dataset: Dataset path within the file (e.g., 'simulation/temperature')

    Returns:
        Dictionary with shape, dtype, compression info, chunk shape, and
        min/max/mean statistics for numeric datasets.
    """
    try:
        validate_non_empty_string(dataset, field="dataset")
        safe_path = validate_read_path(filepath)
        with h5py.File(safe_path, "r") as f:
            ds = f[dataset]
            if not isinstance(ds, h5py.Dataset):
                return {"error": f"'{dataset}' is a group, not a dataset"}

            info: dict[str, Any] = {
                "path": dataset,
                "shape": list(ds.shape),
                "dtype": str(ds.dtype),
                "size_bytes": int(ds.nbytes),
                "chunks": list(ds.chunks) if ds.chunks else None,
                "compression": ds.compression,
                "compression_opts": ds.compression_opts,
                "is_chunked": ds.chunks is not None,
            }

            # Attributes
            attrs = {k: str(v) for k, v in ds.attrs.items()}
            if attrs:
                info["attributes"] = attrs

            # Statistics for numeric datasets
            if np.issubdtype(ds.dtype, np.number):
                # Sample first 10000 elements for large datasets
                total_elements = int(np.prod(ds.shape)) if ds.shape else 0
                if total_elements > 0:
                    if total_elements <= 10000:
                        data = ds[()]
                    else:
                        # Flatten and take first 10000
                        flat_shape = total_elements
                        sample_size = min(10000, flat_shape)
                        data = ds.id.read_direct_chunk is not None  # noqa: F841
                        # Read a slice of the first dimension
                        first_dim_size = ds.shape[0]
                        rows_needed = min(
                            first_dim_size,
                            max(1, sample_size // max(1, int(np.prod(ds.shape[1:]))))
                        )
                        data = ds[:rows_needed]

                    info["statistics"] = {
                        "min": float(np.min(data)),
                        "max": float(np.max(data)),
                        "mean": float(np.mean(data)),
                        "sampled_elements": int(np.size(data)),
                        "total_elements": total_elements,
                    }

            return info
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@hdf5_server.tool()
def check_compression(filepath: str) -> dict[str, Any]:
    """Check compression settings and ratios for all datasets.

    Agent story: When a user wants to know if their file is well-compressed
    or when I need to recommend compression changes.

    Args:
        filepath: Path to the HDF5 file

    Returns:
        Dictionary with per-dataset compression info and overall file
        compression summary.
    """
    try:
        safe_path = validate_read_path(filepath)
        file_size = os.path.getsize(safe_path)
        with h5py.File(safe_path, "r") as f:
            datasets = _collect_datasets(f)
            compression_info = []
            total_raw_size = 0
            compressed_count = 0

            for ds_info in datasets:
                ds = f[ds_info["path"]]
                raw_size = int(ds.nbytes)
                total_raw_size += raw_size

                # Get storage size (actual bytes on disk for this dataset)
                storage_size = ds.id.get_storage_size()

                entry: dict[str, Any] = {
                    "path": ds_info["path"],
                    "compression": ds.compression,
                    "compression_opts": ds.compression_opts,
                    "is_chunked": ds.chunks is not None,
                    "chunks": list(ds.chunks) if ds.chunks else None,
                    "raw_size_bytes": raw_size,
                    "storage_size_bytes": int(storage_size),
                }

                if storage_size > 0 and raw_size > 0:
                    entry["compression_ratio"] = round(raw_size / storage_size, 2)
                else:
                    entry["compression_ratio"] = None

                if ds.compression:
                    compressed_count += 1

                compression_info.append(entry)

            return {
                "filepath": str(safe_path),
                "file_size_bytes": file_size,
                "total_raw_size_bytes": total_raw_size,
                "total_datasets": len(datasets),
                "compressed_datasets": compressed_count,
                "uncompressed_datasets": len(datasets) - compressed_count,
                "datasets": compression_info,
            }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@hdf5_server.tool()
def optimize_chunking(
    filepath: str,
    dataset: str,
    access_pattern: str = "row",
) -> dict[str, Any]:
    """Recommend optimal chunk shape based on dataset dimensions and access pattern.

    Agent story: When a user asks how to speed up reads or when I detect
    suboptimal chunking.

    Args:
        filepath: Path to the HDF5 file
        dataset: Dataset path within the file
        access_pattern: One of 'row', 'column', 'random'. Natural-language
            synonyms are accepted: 'row-wise', 'sequential', 'time-series'
            map to 'row'; 'column-wise', 'columnar' map to 'column';
            'balanced' maps to 'random'.

    Returns:
        Dictionary with current and recommended chunk shapes, plus rationale.
    """
    try:
        access_pattern = _normalize_access_pattern(access_pattern)
        validate_choice(access_pattern, {"row", "column", "random"}, field="access_pattern")
        validate_non_empty_string(dataset, field="dataset")
        safe_path = validate_read_path(filepath)
        with h5py.File(safe_path, "r") as f:
            ds = f[dataset]
            if not isinstance(ds, h5py.Dataset):
                return {"error": f"'{dataset}' is a group, not a dataset"}

            shape = ds.shape
            dtype_size = ds.dtype.itemsize
            ndim = len(shape)

            if ndim == 0:
                return {"error": "Scalar dataset, chunking not applicable"}

            # Target chunk size: ~1MB (standard HDF5 best practice)
            target_bytes = 1 * 1024 * 1024  # 1MB
            target_elements = target_bytes // dtype_size

            # Calculate recommended chunk shape based on access pattern
            if access_pattern == "row":
                # Chunks along first dimension: read full rows efficiently
                # Fix last dimensions, compute first dim chunk size
                if ndim == 1:
                    chunk_size = min(target_elements, shape[0])
                    recommended = (chunk_size,)
                else:
                    trailing_size = int(np.prod(shape[1:]))
                    first_dim = max(1, min(target_elements // max(1, trailing_size), shape[0]))
                    recommended = (first_dim, *shape[1:])

            elif access_pattern == "column":
                # Chunks along last dimension: read full columns efficiently
                if ndim == 1:
                    chunk_size = min(target_elements, shape[0])
                    recommended = (chunk_size,)
                else:
                    leading_size = int(np.prod(shape[:-1]))
                    last_dim = max(1, min(target_elements // max(1, leading_size), shape[-1]))
                    recommended = (*shape[:-1], last_dim)

            else:
                # Balanced chunks: roughly equal along all dimensions
                elements_per_dim = max(1, int(target_elements ** (1.0 / ndim)))
                recommended = tuple(
                    min(elements_per_dim, s) for s in shape
                )

            # Ensure chunk dims don't exceed dataset dims
            recommended = tuple(min(r, s) for r, s in zip(recommended, shape, strict=True))

            recommended_bytes = int(np.prod(recommended)) * dtype_size

            result: dict[str, Any] = {
                "path": dataset,
                "shape": list(shape),
                "dtype": str(ds.dtype),
                "dtype_size_bytes": dtype_size,
                "current_chunks": list(ds.chunks) if ds.chunks else None,
                "is_currently_chunked": ds.chunks is not None,
                "access_pattern": access_pattern,
                "recommended_chunks": list(recommended),
                "recommended_chunk_bytes": recommended_bytes,
                "target_chunk_bytes": target_bytes,
            }

            # Add rationale
            if ds.chunks:
                current_bytes = int(np.prod(ds.chunks)) * dtype_size
                result["current_chunk_bytes"] = current_bytes
                if current_bytes < 64 * 1024:
                    result["rationale"] = (
                        f"Current chunks are too small ({current_bytes} bytes). "
                        f"Excessive metadata overhead. Recommend ~{recommended_bytes} bytes."
                    )
                elif current_bytes > 4 * 1024 * 1024:
                    result["rationale"] = (
                        f"Current chunks are too large ({current_bytes} bytes). "
                        f"Wastes memory on partial reads. Recommend ~{recommended_bytes} bytes."
                    )
                else:
                    result["rationale"] = (
                        f"Current chunk size ({current_bytes} bytes) is reasonable. "
                        f"Recommended {recommended_bytes} bytes for {access_pattern} access."
                    )
            else:
                result["rationale"] = (
                    "Dataset is contiguous (not chunked). Must be re-created with "
                    f"chunks={list(recommended)} for compression and partial I/O support."
                )

            return result
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@hdf5_server.tool()
def analyze_file(filepath: str) -> dict[str, Any]:
    """High-level file analysis: size, datasets, groups, attributes, compression summary.

    Agent story: When a user wants a quick overview of their HDF5 file -- the
    first tool I'd reach for.

    Args:
        filepath: Path to the HDF5 file

    Returns:
        Dictionary with file size, dataset count, group count, root attributes,
        and compression summary.
    """
    try:
        safe_path = validate_read_path(filepath)
        file_size = os.path.getsize(safe_path)
        with h5py.File(safe_path, "r") as f:
            datasets = _collect_datasets(f)
            groups = _collect_groups(f)

            # Root attributes
            root_attrs = {k: str(v) for k, v in f.attrs.items()}

            # Compression summary
            compressed = 0
            total_raw_bytes = 0
            total_stored_bytes = 0
            for ds_info in datasets:
                ds = f[ds_info["path"]]
                total_raw_bytes += int(ds.nbytes)
                total_stored_bytes += int(ds.id.get_storage_size())
                if ds.compression:
                    compressed += 1

            compression_summary: dict[str, int | float] = {
                "compressed_datasets": compressed,
                "uncompressed_datasets": len(datasets) - compressed,
                "total_raw_bytes": total_raw_bytes,
                "total_stored_bytes": total_stored_bytes,
            }
            if total_stored_bytes > 0:
                compression_summary["overall_ratio"] = round(
                    total_raw_bytes / total_stored_bytes, 2
                )

            return {
                "filepath": str(safe_path),
                "file_size_bytes": file_size,
                "total_datasets": len(datasets),
                "total_groups": len(groups),
                "groups": groups,
                "datasets": [d["path"] for d in datasets],
                "root_attributes": root_attrs,
                "compression_summary": compression_summary,
            }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Helpers for the HDF5Expert tool set
# ============================================================================

_H5REPACK_TIMEOUT_SECONDS = 300
_DEFAULT_PLOT_SAMPLE_LIMIT = 200_000  # max elements pulled into matplotlib

# CF-conventions: attribute names that, if present on a variable, count
# toward the lightweight compliance score. Source: CF 1.11 §3.
_CF_RECOMMENDED_DATASET_ATTRS = ("units", "standard_name", "long_name")
_CF_ROOT_CONVENTIONS_ATTR = "Conventions"
_NETCDF4_MARKER_ATTR = "_NCProperties"


def _check_h5repack_available() -> dict[str, Any] | None:
    """Return a structured error dict if h5repack is not on PATH, else None."""
    if shutil.which("h5repack") is None:
        return {
            "error": {
                "type": "missing_dependency",
                "code": "h5repack_not_found",
                "message": (
                    "h5repack not found on PATH. Install HDF5 command-line tools: "
                    "'apt install hdf5-tools' (Debian/Ubuntu), "
                    "'brew install hdf5' (macOS), or 'conda install hdf5'."
                ),
                "field": "h5repack",
                "next_action": "Install hdf5-tools and retry.",
            }
        }
    return None


def _convert_value(value: Any) -> Any:
    """JSON-coerce numpy / bytes / nested values for tool returns."""
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [_convert_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _convert_value(v) for k, v in value.items()}
    return value


def _attribute_metadata(attr_val: Any) -> dict[str, Any]:
    """Compact metadata for a single attribute (dtype, shape, value preview)."""
    info: dict[str, Any] = {
        "dtype": str(getattr(attr_val, "dtype", type(attr_val).__name__)),
    }
    if hasattr(attr_val, "shape"):
        info["shape"] = list(attr_val.shape)
        if getattr(attr_val, "size", 0) <= 32:
            info["value"] = _convert_value(attr_val)
        else:
            info["note"] = f"array of {attr_val.size} elements omitted"
    else:
        info["value"] = _convert_value(attr_val)
    return info


def _dataset_metadata(ds: h5py.Dataset) -> dict[str, Any]:
    """Extract dataset-specific metadata: layout, filters, statistics."""
    info: dict[str, Any] = {
        "object_type": "dataset",
        "shape": list(ds.shape),
        "dtype": str(ds.dtype),
        "size": int(ds.size),
        "size_bytes": int(ds.dtype.itemsize) * int(ds.size),
        "maxshape": list(ds.maxshape),
        "chunks": list(ds.chunks) if ds.chunks else None,
        "is_chunked": ds.chunks is not None,
        "compression": ds.compression,
        "compression_opts": ds.compression_opts,
        "shuffle": bool(ds.shuffle),
        "fletcher32": bool(ds.fletcher32),
        "scaleoffset": ds.scaleoffset,
        "storage_size_bytes": int(ds.id.get_storage_size()),
    }
    if ds.fillvalue is not None:
        info["fillvalue"] = _convert_value(ds.fillvalue)
    if info["size"] > 0 and info["size"] <= 10_000_000:
        try:
            if np.issubdtype(ds.dtype, np.number):
                data = ds[()]
                info["statistics"] = {
                    "min": float(np.min(data)),
                    "max": float(np.max(data)),
                    "mean": float(np.mean(data)),
                }
        except Exception as exc:  # noqa: BLE001 - statistics are best-effort
            info["statistics_note"] = f"statistics unavailable: {exc}"
    elif info["size"] > 10_000_000:
        info["statistics_note"] = (
            f"dataset too large for automatic statistics ({info['size']} elements)"
        )
    return info


def _group_metadata(grp: h5py.Group, object_path: str) -> dict[str, Any]:
    """Extract group-specific metadata: member list, root flag."""
    return {
        "object_type": "file_root" if object_path == "/" else "group",
        "members": list(grp.keys()),
        "num_members": len(grp),
    }


def _link_metadata(link_info: Any, object_path: str) -> dict[str, Any] | None:
    """Return link-specific metadata if link_info is a soft/external link."""
    if isinstance(link_info, h5py.SoftLink):
        return {
            "object_type": "soft_link",
            "name": object_path,
            "target": link_info.path,
        }
    if isinstance(link_info, h5py.ExternalLink):
        return {
            "object_type": "external_link",
            "name": object_path,
            "filename": link_info.filename,
            "target": link_info.path,
        }
    return None


def _resolve_output_path(
    input_path: Path, suffix: str, requested: Optional[str], *, field: str
) -> Path:
    """Pick + validate the output path for a write-producing tool.

    Tools default to ``<input_stem><suffix><ext>`` next to the input. An
    explicit ``requested`` path overrides that default. Either way the
    resolved path is run through ``validate_write_path`` so it lands inside
    an allowed root and never resolves through a symlink.
    """
    if requested:
        candidate = requested
    else:
        candidate = str(input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}"))
    return validate_write_path(candidate, field=field)


# ============================================================================
# HDF5Expert tools
# ============================================================================


@hdf5_server.tool()
def get_object_metadata(filepath: str, object_path: str) -> dict[str, Any]:
    """Inspect an HDF5 object: returns shape, dtype, chunks, filters,
    attributes, and (for groups) member lists. Works on datasets, groups,
    committed datatypes, and soft/external links.

    Agent story: the first tool to call when the user names an object inside
    a known file, before deciding whether to rechunk, refilter, visualize,
    or read its data.

    Args:
        filepath: Path to the HDF5 file.
        object_path: Path to the object within the file (e.g. "/group/dataset").

    Returns:
        Dict with object_type and type-specific fields. Includes attributes
        when present.
    """
    try:
        validate_non_empty_string(object_path, field="object_path")
        safe_path = validate_read_path(filepath)
        with h5py.File(safe_path, "r") as f:
            normalized = object_path if object_path.startswith("/") else "/" + object_path
            if normalized != "/":
                parent_path = "/".join(normalized.rstrip("/").split("/")[:-1]) or "/"
                obj_name = normalized.rstrip("/").split("/")[-1]
                if parent_path in f and obj_name:
                    parent = f[parent_path]
                    link_info = parent.get(obj_name, getlink=True)
                    link_meta = _link_metadata(link_info, normalized)
                    if link_meta is not None:
                        return {"filepath": str(safe_path), "name": normalized, **link_meta}

            if normalized not in f:
                return {
                    "error": {
                        "type": "object_not_found",
                        "code": "object_not_found",
                        "message": f"Object '{normalized}' not found in {safe_path}",
                        "field": "object_path",
                        "next_action": (
                            "Call list_datasets or analyze_file to enumerate "
                            "available objects."
                        ),
                    }
                }

            obj = f[normalized]
            base: dict[str, Any] = {"filepath": str(safe_path), "name": normalized}
            if hasattr(obj, "attrs") and len(obj.attrs):
                base["attributes"] = {
                    name: _attribute_metadata(obj.attrs[name]) for name in obj.attrs
                }

            if isinstance(obj, h5py.Dataset):
                base.update(_dataset_metadata(obj))
            elif isinstance(obj, h5py.Group):
                base.update(_group_metadata(obj, normalized))
            elif isinstance(obj, h5py.Datatype):
                base.update({"object_type": "committed_datatype", "dtype": str(obj.dtype)})
            else:
                base["object_type"] = "unknown"
            return base
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@hdf5_server.tool()
def rechunk_dataset(
    filepath: str,
    object_path: str,
    chunk_dims: Optional[str] = None,
    chunk_adjustment: Optional[Literal["larger", "smaller", "half", "double"]] = None,
    make_contiguous: bool = False,
    output_filepath: Optional[str] = None,
) -> dict[str, Any]:
    """Change the chunk layout of a dataset by invoking h5repack. Always
    writes a NEW file; never modifies the input in place. If output_filepath
    is omitted, writes alongside the input with a .rechunked suffix.

    Specify exactly one of: chunk_dims (e.g. "100x200"), chunk_adjustment
    ("larger" / "smaller" / "half" / "double"), or make_contiguous=True.

    Agent story: call after get_object_metadata reports a chunk shape that
    is misaligned with the access pattern or far outside the 10 KB - 1 MB
    target band. Surface the planned change before invoking.

    Args:
        filepath: Path to the input HDF5 file.
        object_path: Dataset path within the file.
        chunk_dims: Exact chunk dimensions, "x"-separated (e.g. "100x200").
        chunk_adjustment: High-level adjustment relative to current chunks.
        make_contiguous: If True, remove chunking and write contiguous layout.
        output_filepath: Explicit output path. Defaults to a sibling of the
            input named <stem>.rechunked<ext>.

    Returns:
        Dict with success flag, output path, original_chunks, new_chunks,
        and a human-readable message. Failure cases return a structured error.
    """
    try:
        validate_non_empty_string(object_path, field="object_path")
        safe_input = validate_read_path(filepath)

        with h5py.File(safe_input, "r") as f:
            if object_path not in f:
                return {
                    "error": {
                        "type": "object_not_found",
                        "code": "object_not_found",
                        "message": f"Dataset '{object_path}' not found in {safe_input}",
                        "field": "object_path",
                        "next_action": "Call list_datasets for the available paths.",
                    }
                }
            obj = f[object_path]
            if not isinstance(obj, h5py.Dataset):
                return {
                    "error": {
                        "type": "wrong_object_type",
                        "code": "not_a_dataset",
                        "message": f"'{object_path}' is not a dataset.",
                        "field": "object_path",
                        "next_action": "Pass the path of a dataset, not a group.",
                    }
                }
            current_chunks = list(obj.chunks) if obj.chunks else None
            dataset_shape = list(obj.shape)

        n_layout_args = sum(
            [
                bool(chunk_dims),
                bool(chunk_adjustment),
                bool(make_contiguous),
            ]
        )
        if n_layout_args != 1:
            return {
                "error": {
                    "type": "invalid_argument",
                    "code": "ambiguous_layout_request",
                    "message": (
                        "Specify exactly one of chunk_dims, chunk_adjustment, "
                        "or make_contiguous=True."
                    ),
                    "field": "chunk_dims",
                    "next_action": "Resubmit with exactly one layout parameter.",
                }
            }

        if make_contiguous:
            layout_arg = f"{object_path}:CONTI"
            planned_chunks: Any = None
        elif chunk_dims:
            layout_arg = f"{object_path}:CHUNK={chunk_dims}"
            try:
                planned_chunks = [int(x) for x in chunk_dims.split("x")]
            except ValueError:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "bad_chunk_dims",
                        "message": f"chunk_dims must be 'x'-separated ints (got {chunk_dims!r}).",
                        "field": "chunk_dims",
                        "next_action": "Use the form '100x200' or '10x20x30'.",
                    }
                }
        else:
            assert chunk_adjustment is not None
            if current_chunks is None:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "contiguous_input",
                        "message": (
                            "Dataset is contiguous; cannot apply a chunk_adjustment. "
                            "Use chunk_dims to specify a starting layout instead."
                        ),
                        "field": "chunk_adjustment",
                        "next_action": "Use chunk_dims instead.",
                    }
                }
            multiplier = 2.0 if chunk_adjustment in ("larger", "double") else 0.5
            planned = []
            for chunk_dim, ds_dim in zip(current_chunks, dataset_shape, strict=True):
                new_dim = max(1, int(round(chunk_dim * multiplier)))
                new_dim = min(new_dim, int(ds_dim))
                planned.append(new_dim)
            planned_chunks = planned
            layout_arg = f"{object_path}:CHUNK={'x'.join(str(d) for d in planned)}"

        suffix = ".rechunked"
        out_path = _resolve_output_path(
            Path(safe_input), suffix, output_filepath, field="output_filepath"
        )
        if out_path.exists():
            return {
                "error": {
                    "type": "output_exists",
                    "code": "output_exists",
                    "message": f"Output file already exists: {out_path}",
                    "field": "output_filepath",
                    "next_action": "Choose a new path or delete the existing file.",
                }
            }

        h5repack_error = _check_h5repack_available()
        if h5repack_error is not None:
            return h5repack_error

        cmd = ["h5repack", "-i", str(safe_input), "-o", str(out_path), "-l", layout_arg]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_H5REPACK_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": (
                    f"h5repack failed (exit {result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                ),
                "command": " ".join(cmd),
            }
        if not out_path.exists():
            return {
                "success": False,
                "error": "h5repack returned 0 but no output file was created.",
                "command": " ".join(cmd),
            }

        try:
            with h5py.File(out_path, "r") as f:
                new_chunks = list(f[object_path].chunks) if f[object_path].chunks else None
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"Output file created but unreadable: {exc}",
                "output_filepath": str(out_path),
            }

        return {
            "success": True,
            "output_filepath": str(out_path),
            "object_path": object_path,
            "dataset_shape": dataset_shape,
            "original_chunks": current_chunks,
            "new_chunks": new_chunks,
            "planned_chunks": planned_chunks,
            "message": (
                f"Rechunked '{object_path}' from {current_chunks} to {new_chunks} "
                f"in {out_path}."
            ),
        }
    except FilePolicyError as e:
        return e.to_result()
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"h5repack timed out after {_H5REPACK_TIMEOUT_SECONDS}s.",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


@hdf5_server.tool()
def apply_filter(
    filepath: str,
    object_path: str,
    filter_type: Optional[
        Literal["gzip", "szip", "shuffle", "fletcher32", "nbit", "scaleoffset", "none"]
    ] = None,
    compression_level: Optional[int] = None,
    szip_options: Optional[str] = None,
    scaleoffset_params: Optional[str] = None,
    remove_all_filters: bool = False,
    output_filepath: Optional[str] = None,
) -> dict[str, Any]:
    """Add, change, or remove a compression/filter on a dataset via h5repack.
    Always writes a NEW file.

    Agent story: invoked after get_object_metadata shows uncompressed numeric
    data or a filter mismatch (e.g. gzip-9 where the access pattern would
    benefit from lz4-equivalent throughput).

    Args:
        filepath: Input HDF5 file path.
        object_path: Dataset path within the file.
        filter_type: One of gzip, szip, shuffle, fletcher32, nbit, scaleoffset, none.
        compression_level: For gzip only, 1-9 (default 6).
        szip_options: For szip, "pixels_per_block,coding" (e.g. "8,NN").
        scaleoffset_params: For scaleoffset, "scale_factor,scale_type" (e.g. "3,DS").
        remove_all_filters: If True, strip every filter (alias for filter_type='none').
        output_filepath: Explicit output path. Defaults to <stem>.refiltered<ext>.

    Returns:
        Dict with success flag, output_filepath, original_filters, new_filters,
        and a human-readable message.
    """
    try:
        validate_non_empty_string(object_path, field="object_path")
        safe_input = validate_read_path(filepath)

        import h5py.h5z as h5z

        filter_availability = {
            "gzip": h5z.filter_avail(h5z.FILTER_DEFLATE),
            "shuffle": h5z.filter_avail(h5z.FILTER_SHUFFLE),
            "fletcher32": h5z.filter_avail(h5z.FILTER_FLETCHER32),
            "szip": h5z.filter_avail(h5z.FILTER_SZIP),
            "nbit": h5z.filter_avail(h5z.FILTER_NBIT),
            "scaleoffset": h5z.filter_avail(h5z.FILTER_SCALEOFFSET),
        }

        with h5py.File(safe_input, "r") as f:
            if object_path not in f:
                return {
                    "error": {
                        "type": "object_not_found",
                        "code": "object_not_found",
                        "message": f"Dataset '{object_path}' not found.",
                        "field": "object_path",
                        "next_action": "Call list_datasets for available paths.",
                    }
                }
            obj = f[object_path]
            if not isinstance(obj, h5py.Dataset):
                return {
                    "error": {
                        "type": "wrong_object_type",
                        "code": "not_a_dataset",
                        "message": f"'{object_path}' is not a dataset.",
                        "field": "object_path",
                        "next_action": "Pass the path of a dataset.",
                    }
                }
            original_filters = {
                "compression": obj.compression,
                "compression_opts": obj.compression_opts,
                "shuffle": bool(obj.shuffle),
                "fletcher32": bool(obj.fletcher32),
                "scaleoffset": obj.scaleoffset,
            }
            dataset_shape = list(obj.shape)

        effective_filter = "none" if remove_all_filters else filter_type
        if effective_filter is None:
            return {
                "error": {
                    "type": "invalid_argument",
                    "code": "no_filter_specified",
                    "message": "Set filter_type or remove_all_filters=True.",
                    "field": "filter_type",
                    "next_action": "Pick a filter or pass remove_all_filters=True.",
                }
            }

        if effective_filter in filter_availability and not filter_availability[effective_filter]:
            return {
                "error": {
                    "type": "missing_dependency",
                    "code": f"filter_unavailable:{effective_filter}",
                    "message": (
                        f"Filter '{effective_filter}' is not available in this "
                        "HDF5 build."
                    ),
                    "field": "filter_type",
                    "next_action": "Rebuild HDF5 with the filter enabled or pick another.",
                    "details": {"filter_availability": filter_availability},
                }
            }

        if effective_filter == "none":
            filter_arg = f"{object_path}:NONE"
            filter_desc = "remove all filters"
        elif effective_filter == "gzip":
            level = compression_level if compression_level is not None else 6
            if not 1 <= level <= 9:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "bad_gzip_level",
                        "message": f"gzip level {level} out of range (1-9).",
                        "field": "compression_level",
                        "next_action": "Use a value between 1 and 9.",
                    }
                }
            filter_arg = f"{object_path}:GZIP={level}"
            filter_desc = f"GZIP level {level}"
        elif effective_filter == "szip":
            if not szip_options:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "missing_szip_options",
                        "message": "szip requires szip_options (e.g. '8,NN').",
                        "field": "szip_options",
                        "next_action": "Provide pixels_per_block,coding.",
                    }
                }
            filter_arg = f"{object_path}:SZIP={szip_options}"
            filter_desc = f"SZIP ({szip_options})"
        elif effective_filter == "shuffle":
            filter_arg = f"{object_path}:SHUF"
            filter_desc = "Shuffle"
        elif effective_filter == "fletcher32":
            filter_arg = f"{object_path}:FLET"
            filter_desc = "Fletcher32 checksum"
        elif effective_filter == "nbit":
            filter_arg = f"{object_path}:NBIT"
            filter_desc = "N-bit"
        elif effective_filter == "scaleoffset":
            if not scaleoffset_params:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "missing_scaleoffset_params",
                        "message": (
                            "scaleoffset requires scaleoffset_params (e.g. '3,DS')."
                        ),
                        "field": "scaleoffset_params",
                        "next_action": "Provide scale_factor,scale_type.",
                    }
                }
            filter_arg = f"{object_path}:SOFF={scaleoffset_params}"
            filter_desc = f"Scale-offset ({scaleoffset_params})"
        else:
            return {
                "error": {
                    "type": "invalid_argument",
                    "code": "unknown_filter",
                    "message": f"Unknown filter_type: {effective_filter!r}.",
                    "field": "filter_type",
                    "next_action": (
                        "Pick one of gzip, szip, shuffle, fletcher32, nbit, "
                        "scaleoffset, none."
                    ),
                }
            }

        suffix = ".refiltered"
        out_path = _resolve_output_path(
            Path(safe_input), suffix, output_filepath, field="output_filepath"
        )
        if out_path.exists():
            return {
                "error": {
                    "type": "output_exists",
                    "code": "output_exists",
                    "message": f"Output file already exists: {out_path}",
                    "field": "output_filepath",
                    "next_action": "Choose a new path or delete the existing file.",
                }
            }

        h5repack_error = _check_h5repack_available()
        if h5repack_error is not None:
            return h5repack_error

        cmd = ["h5repack", "-i", str(safe_input), "-o", str(out_path), "-f", filter_arg]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_H5REPACK_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            return {
                "success": False,
                "error": (
                    f"h5repack failed (exit {result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                ),
                "command": " ".join(cmd),
            }

        try:
            with h5py.File(out_path, "r") as f:
                new_obj = f[object_path]
                new_filters = {
                    "compression": new_obj.compression,
                    "compression_opts": new_obj.compression_opts,
                    "shuffle": bool(new_obj.shuffle),
                    "fletcher32": bool(new_obj.fletcher32),
                    "scaleoffset": new_obj.scaleoffset,
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"Output created but unreadable: {exc}",
                "output_filepath": str(out_path),
            }

        return {
            "success": True,
            "output_filepath": str(out_path),
            "object_path": object_path,
            "dataset_shape": dataset_shape,
            "original_filters": original_filters,
            "new_filters": new_filters,
            "message": f"Applied {filter_desc} to '{object_path}' in {out_path}.",
        }
    except FilePolicyError as e:
        return e.to_result()
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"h5repack timed out after {_H5REPACK_TIMEOUT_SECONDS}s.",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


@hdf5_server.tool()
def visualize_dataset(
    filepath: str,
    object_path: str,
    save_path: Optional[str] = None,
    plot_type: Literal["auto", "line", "hist", "imshow", "pcolormesh"] = "auto",
    max_points: int = _DEFAULT_PLOT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Render a PNG plot of an HDF5 dataset using matplotlib (Agg backend).

    1D datasets default to a line plot; 2D default to imshow/pcolormesh.
    Larger datasets are downsampled uniformly to max_points elements before
    plotting; the result records the actual sampled shape.

    Agent story: invoked when the user wants to "see" a dataset before doing
    anything else with it. Cheap, lossy, never authoritative.

    Args:
        filepath: HDF5 file path.
        object_path: Dataset path within the file.
        save_path: Output PNG path. Defaults to <input_dir>/<obj_basename>_plot.png.
        plot_type: 'auto' picks by dimensionality. Explicit choices override.
        max_points: Cap on elements rendered. Default ~200k.

    Returns:
        Dict with success, save_path, plot_type, shape, sampled_shape, message.
    """
    try:
        validate_non_empty_string(object_path, field="object_path")
        safe_input = validate_read_path(filepath)
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        with h5py.File(safe_input, "r") as f:
            if object_path not in f:
                return {
                    "error": {
                        "type": "object_not_found",
                        "code": "object_not_found",
                        "message": f"Object '{object_path}' not found.",
                        "field": "object_path",
                        "next_action": "Call list_datasets for available paths.",
                    }
                }
            obj = f[object_path]
            if not isinstance(obj, h5py.Dataset):
                return {
                    "error": {
                        "type": "wrong_object_type",
                        "code": "not_a_dataset",
                        "message": f"'{object_path}' is not a dataset.",
                        "field": "object_path",
                        "next_action": "Pass the path of a dataset.",
                    }
                }
            shape = tuple(int(s) for s in obj.shape)
            ndim = len(shape)
            total = int(np.prod(shape)) if shape else 0
            if ndim == 0 or total == 0:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "empty_or_scalar",
                        "message": "Cannot plot a scalar or empty dataset.",
                        "field": "object_path",
                        "next_action": "Pick a dataset with one or two dimensions.",
                    }
                }
            if ndim not in (1, 2):
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "unsupported_rank",
                        "message": (
                            f"visualize_dataset supports 1D and 2D datasets; "
                            f"got rank {ndim}."
                        ),
                        "field": "object_path",
                        "next_action": "Slice before visualizing.",
                    }
                }
            if not np.issubdtype(obj.dtype, np.number):
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "non_numeric_dtype",
                        "message": f"Cannot plot non-numeric dtype {obj.dtype!s}.",
                        "field": "object_path",
                        "next_action": "Pick a numeric dataset.",
                    }
                }

            if total <= max_points:
                data = obj[()]
                sampled_shape = shape
            elif ndim == 1:
                stride = max(1, total // max_points)
                data = obj[::stride]
                sampled_shape = tuple(data.shape)
            else:
                stride0 = max(1, shape[0] * shape[1] // max_points)
                stride0 = max(1, int(np.sqrt(stride0)))
                stride1 = stride0
                data = obj[::stride0, ::stride1]
                sampled_shape = tuple(data.shape)

        if plot_type == "auto":
            plot_type = "line" if ndim == 1 else "imshow"

        if save_path is None:
            obj_basename = object_path.strip("/").replace("/", "_") or "root"
            default_save = Path(safe_input).with_name(f"{obj_basename}_plot.png")
            safe_save = validate_write_path(str(default_save), field="save_path")
        else:
            safe_save = validate_write_path(save_path, field="save_path")

        fig, ax = plt.subplots(figsize=(10, 6))
        try:
            if plot_type == "line":
                ax.plot(data)
                ax.set_xlabel("index")
                ax.set_ylabel("value")
            elif plot_type == "hist":
                ax.hist(np.asarray(data).ravel(), bins=64)
                ax.set_xlabel("value")
                ax.set_ylabel("count")
            elif plot_type == "imshow":
                im = ax.imshow(data, aspect="auto")
                fig.colorbar(im, ax=ax)
            elif plot_type == "pcolormesh":
                pcm = ax.pcolormesh(data)
                fig.colorbar(pcm, ax=ax)
            else:
                return {
                    "error": {
                        "type": "invalid_argument",
                        "code": "unknown_plot_type",
                        "message": f"Unknown plot_type {plot_type!r}.",
                        "field": "plot_type",
                        "next_action": "Use auto/line/hist/imshow/pcolormesh.",
                    }
                }
            ax.set_title(object_path)
            fig.tight_layout()
            fig.savefig(safe_save, dpi=100, bbox_inches="tight")
        finally:
            plt.close(fig)

        return {
            "success": True,
            "save_path": str(safe_save),
            "object_path": object_path,
            "plot_type": plot_type,
            "shape": list(shape),
            "sampled_shape": list(sampled_shape),
            "message": (
                f"Wrote {plot_type} plot of '{object_path}' (sampled "
                f"{sampled_shape}) to {safe_save}."
            ),
        }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


@hdf5_server.tool()
def check_cf_compliance(filepath: str) -> dict[str, Any]:
    """Lightweight check of CF-conventions metadata on an HDF5/NetCDF4 file.

    This does NOT replace the IOOS compliance-checker; it inspects the root
    'Conventions' attribute, the NetCDF4 marker, and the presence of CF's
    most-cited per-variable attributes (units, standard_name, long_name) on
    every dataset. It returns a score (% of datasets that carry at least
    units AND a standard_name OR long_name) plus a list of issues.

    Agent story: a fast pre-flight before publishing a NetCDF4 file. If the
    score is low, advise running the proper compliance-checker out-of-band;
    if it is high, the file probably passes most CF checks.

    Args:
        filepath: HDF5 / NetCDF4 file path.

    Returns:
        Dict with status, file_format, declared_conventions, score,
        issue_counts, issues, and total_datasets_checked.
    """
    try:
        safe_path = validate_read_path(filepath)
        with h5py.File(safe_path, "r") as f:
            is_netcdf4 = _NETCDF4_MARKER_ATTR in f.attrs
            declared = None
            if _CF_ROOT_CONVENTIONS_ATTR in f.attrs:
                raw = f.attrs[_CF_ROOT_CONVENTIONS_ATTR]
                declared = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, bytes)
                    else str(raw)
                )

            datasets = _collect_datasets(f)
            issues: list[dict[str, Any]] = []
            datasets_passing = 0
            for ds_info in datasets:
                path = ds_info["path"]
                ds = f[path]
                attrs = set(ds.attrs)
                missing = [a for a in _CF_RECOMMENDED_DATASET_ATTRS if a not in attrs]
                has_units = "units" in attrs
                has_name = "standard_name" in attrs or "long_name" in attrs
                if has_units and has_name:
                    datasets_passing += 1
                else:
                    severity = "high" if not has_units else "medium"
                    issues.append(
                        {
                            "severity": severity,
                            "path": path,
                            "missing": missing,
                            "message": (
                                f"Dataset '{path}' missing CF attrs: "
                                f"{', '.join(missing) or '(name attrs)'}"
                            ),
                        }
                    )

            if not is_netcdf4 and declared is None:
                issues.insert(
                    0,
                    {
                        "severity": "high",
                        "path": "/",
                        "missing": [_CF_ROOT_CONVENTIONS_ATTR, _NETCDF4_MARKER_ATTR],
                        "message": (
                            "File is not NetCDF4-formatted (no _NCProperties) and "
                            "declares no Conventions. CF applies only to NetCDF4 "
                            "files."
                        ),
                    },
                )

            total = len(datasets)
            percent = round(100.0 * datasets_passing / total, 1) if total else 0.0
            issue_counts = {"high": 0, "medium": 0, "low": 0}
            for issue in issues:
                issue_counts[issue["severity"]] += 1

            return {
                "status": "ok",
                "filepath": str(safe_path),
                "file_format": "NetCDF4" if is_netcdf4 else "HDF5",
                "declared_conventions": declared,
                "total_datasets_checked": total,
                "datasets_passing_basic_cf": datasets_passing,
                "score_percent": percent,
                "issue_counts": issue_counts,
                "issues": issues,
                "note": (
                    "Lightweight static check. For an authoritative report, run "
                    "the IOOS compliance-checker out-of-band."
                ),
            }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


@hdf5_server.tool()
def consult_skill(topic: str) -> dict[str, Any]:
    """Retrieve in-depth guidance from the bundled HDF5 skill library.

    The HDF5Expert's signature contains a short index of every available
    skill by name. When the expert needs depth on one of them — chunking,
    filters, VFDs, SWMR, VDS, CF compliance, etc. — call this tool with
    either the exact skill name (e.g. 'hdf5-chunking') or a free-form
    topic phrase (e.g. 'rechunk my dataset to align with column access').
    The exact-name path returns the requested skill's body directly. The
    free-form path returns the top-matching skill plus a list of
    alternatives in descending relevance.

    Agent story: the on-demand depth-fetch. The signature's index tells
    the expert *what exists*; this tool reveals *what each one says*.

    Args:
        topic: A bundled skill name, or a natural-language phrase describing
            the topic the expert needs guidance on.

    Returns:
        Dict with skill_name, description, body (full SKILL.md text),
        and alternatives (list of (name, score) pairs the matcher also
        scored). On a totally unrelated query the alternatives list is
        empty and an error dict is returned instead.
    """
    # Lazy import: clio_agent.experts.hdf5_skills lives under the experts
    # package, whose __init__ pulls in modules that mount this server.
    # Importing at module load time creates a cycle; importing inside the
    # tool body sidesteps it without changing call semantics.
    from clio_agent.experts.hdf5_skills import (
        SkillNotFoundError,
        list_skills,
        load_skill,
        match_skills,
    )

    try:
        if not isinstance(topic, str) or not topic.strip():
            return {
                "error": {
                    "type": "invalid_argument",
                    "code": "empty_topic",
                    "message": "topic must be a non-empty string.",
                    "field": "topic",
                    "next_action": "Provide a skill name or a topic phrase.",
                }
            }
        topic = topic.strip()
        try:
            body = load_skill(topic)
            summary = next(s for s in list_skills() if s["name"] == topic)
            return {
                "skill_name": topic,
                "description": summary["description"],
                "body": body,
                "alternatives": [],
                "matched_by": "exact_name",
            }
        except SkillNotFoundError:
            pass

        matches = match_skills(topic, top_k=5)
        if not matches:
            return {
                "error": {
                    "type": "no_match",
                    "code": "no_skill_match",
                    "message": f"No bundled skill matched topic {topic!r}.",
                    "field": "topic",
                    "next_action": (
                        "Try a different phrasing or call list_skills via "
                        "the loader. Known skills are listed in the "
                        "HDF5Expert signature index."
                    ),
                }
            }
        top_name, _top_score = matches[0]
        summary = next(s for s in list_skills() if s["name"] == top_name)
        return {
            "skill_name": top_name,
            "description": summary["description"],
            "body": load_skill(top_name),
            "alternatives": [
                {"name": name, "score": score} for name, score in matches[1:]
            ],
            "matched_by": "topic_match",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": {"type": "internal_error", "message": str(e)}}
