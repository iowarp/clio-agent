"""
HDF5 MCP Server for CLIO Agent

A FastMCP server providing 5 curated tools for HDF5 file analysis
using real h5py operations. Each tool is designed for a specific
agent use case (agent story) in the CLIO data analysis workflow.

Tools:
    - list_datasets: Discover datasets in an HDF5 file
    - analyze_dataset: Deep analysis of a specific dataset
    - check_compression: Compression audit across all datasets
    - optimize_chunking: Chunk shape recommendations for access patterns
    - analyze_file: High-level file overview

Usage:
    >>> from clio_agent.tools.servers.hdf5_server import hdf5_server
    >>> # Use with FastMCP Client for in-memory testing
    >>> from fastmcp import Client
    >>> async with Client(hdf5_server) as client:
    ...     result = await client.call_tool("analyze_file", {"filepath": "data.h5"})
"""

import os
from typing import Any

import h5py
import numpy as np
from fastmcp import FastMCP

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
        with h5py.File(filepath, "r") as f:
            datasets = _collect_datasets(f)
            return {
                "filepath": filepath,
                "total_datasets": len(datasets),
                "datasets": datasets,
            }
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
        with h5py.File(filepath, "r") as f:
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
        file_size = os.path.getsize(filepath)
        with h5py.File(filepath, "r") as f:
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
                "filepath": filepath,
                "file_size_bytes": file_size,
                "total_raw_size_bytes": total_raw_size,
                "total_datasets": len(datasets),
                "compressed_datasets": compressed_count,
                "uncompressed_datasets": len(datasets) - compressed_count,
                "datasets": compression_info,
            }
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
        access_pattern: Access pattern - 'row' (sequential along first dim),
            'column' (sequential along last dim), or 'random' (balanced)

    Returns:
        Dictionary with current and recommended chunk shapes, plus rationale.
    """
    try:
        with h5py.File(filepath, "r") as f:
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

            elif access_pattern == "random":
                # Balanced chunks: roughly equal along all dimensions
                elements_per_dim = max(1, int(target_elements ** (1.0 / ndim)))
                recommended = tuple(
                    min(elements_per_dim, s) for s in shape
                )
            else:
                return {"error": f"Unknown access pattern: '{access_pattern}'. Use 'row', 'column', or 'random'."}

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
        file_size = os.path.getsize(filepath)
        with h5py.File(filepath, "r") as f:
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

            compression_summary = {
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
                "filepath": filepath,
                "file_size_bytes": file_size,
                "total_datasets": len(datasets),
                "total_groups": len(groups),
                "groups": groups,
                "datasets": [d["path"] for d in datasets],
                "root_attributes": root_attrs,
                "compression_summary": compression_summary,
            }
    except Exception as e:
        return {"error": str(e)}
