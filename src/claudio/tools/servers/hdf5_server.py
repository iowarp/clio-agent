#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp>=0.1.0",
#   "h5py>=3.10.0",
# ]
# ///

"""
ClaudIO HDF5 MCP Server

FastMCP server exposing HDF5 analysis and optimization tools.
Provides scientific data file operations via MCP protocol.

Tools:
- hdf5_analyze: Analyze HDF5 file structure
- hdf5_get_info: Get basic file information
- hdf5_list_datasets: List all datasets in file
- hdf5_check_compression: Check compression settings

Usage:
    # Run server
    $ uv run src/claudio/tools/servers/hdf5_server.py

    # Or with custom port
    $ uv run src/claudio/tools/servers/hdf5_server.py --port 8001

    # Connect from client
    >>> from fastmcp import Client
    >>> client = Client({"mcpServers": {"hdf5": {"url": "http://localhost:8000/mcp"}}})
    >>> result = await client.call_tool("hdf5_analyze", {"filepath": "/data/file.h5"})
"""

from fastmcp import FastMCP
from typing import Optional
import sys
from pathlib import Path

# Create MCP server
mcp = FastMCP(
    name="HDF5 Scientific Data Tools",
    description="MCP server for HDF5 file analysis and optimization"
)


# ============================================================================
# HDF5 ANALYSIS TOOLS
# ============================================================================

@mcp.tool
def hdf5_analyze(filepath: str) -> dict:
    """Analyze HDF5 file structure, compression, and performance characteristics.

    Provides comprehensive analysis including:
    - File size and compression ratio
    - Dataset structure and chunking
    - Compression algorithms used
    - Performance recommendations

    Args:
        filepath: Absolute path to HDF5 file

    Returns:
        Analysis results dict with:
            - file_size_mb: File size in megabytes
            - compression_ratio: Achieved compression ratio
            - datasets: List of dataset info
            - recommendations: Optimization suggestions
    """
    try:
        import h5py
        import os

        if not os.path.exists(filepath):
            return {
                "error": f"File not found: {filepath}",
                "filepath": filepath
            }

        file_size = os.path.getsize(filepath) / (1024**2)  # MB

        with h5py.File(filepath, 'r') as f:
            datasets = []

            def visit_datasets(name, obj):
                if isinstance(obj, h5py.Dataset):
                    compression = obj.compression or "none"
                    compression_opts = obj.compression_opts or 0

                    datasets.append({
                        "name": name,
                        "shape": list(obj.shape) if obj.shape else [],
                        "dtype": str(obj.dtype),
                        "compression": compression,
                        "compression_level": compression_opts,
                        "chunks": list(obj.chunks) if obj.chunks else None,
                        "size_mb": obj.nbytes / (1024**2)
                    })

            f.visititems(visit_datasets)

            # Calculate compression ratio (rough estimate)
            total_uncompressed = sum(d['size_mb'] for d in datasets)
            compression_ratio = total_uncompressed / file_size if file_size > 0 else 1.0

            # Generate recommendations
            recommendations = []
            for ds in datasets:
                if ds['compression'] == 'none':
                    recommendations.append(
                        f"Dataset '{ds['name']}': No compression - consider gzip-6 or blosc"
                    )
                if ds['chunks'] is None and ds['size_mb'] > 100:
                    recommendations.append(
                        f"Dataset '{ds['name']}': Not chunked - enable chunking for parallel I/O"
                    )

            return {
                "filepath": filepath,
                "file_size_mb": round(file_size, 2),
                "compression_ratio": round(compression_ratio, 2),
                "num_datasets": len(datasets),
                "datasets": datasets,
                "recommendations": recommendations if recommendations else ["File is well-optimized"]
            }

    except ImportError:
        return {
            "error": "h5py not available - install with: uv add h5py",
            "filepath": filepath,
            "fallback_mode": True
        }
    except Exception as e:
        return {
            "error": str(e),
            "filepath": filepath
        }


@mcp.tool
def hdf5_get_info(filepath: str) -> dict:
    """Get basic information about an HDF5 file.

    Quick lightweight check without deep analysis.

    Args:
        filepath: Path to HDF5 file

    Returns:
        Basic file information
    """
    try:
        import h5py
        import os

        if not os.path.exists(filepath):
            return {"error": f"File not found: {filepath}"}

        file_size = os.path.getsize(filepath) / (1024**2)  # MB

        with h5py.File(filepath, 'r') as f:
            num_datasets = len(list(f.keys()))

            return {
                "filepath": filepath,
                "exists": True,
                "file_size_mb": round(file_size, 2),
                "num_top_level_datasets": num_datasets,
                "hdf5_version": h5py.version.hdf5_version
            }

    except Exception as e:
        return {
            "error": str(e),
            "filepath": filepath
        }


@mcp.tool
def hdf5_list_datasets(filepath: str, max_depth: int = 3) -> dict:
    """List all datasets in HDF5 file.

    Args:
        filepath: Path to HDF5 file
        max_depth: Maximum nesting depth to explore

    Returns:
        List of dataset paths and basic info
    """
    try:
        import h5py

        with h5py.File(filepath, 'r') as f:
            datasets = []

            def visit(name, obj):
                if isinstance(obj, h5py.Dataset):
                    datasets.append({
                        "path": name,
                        "shape": list(obj.shape) if obj.shape else [],
                        "dtype": str(obj.dtype)
                    })

            f.visititems(visit)

            return {
                "filepath": filepath,
                "datasets": datasets,
                "count": len(datasets)
            }

    except Exception as e:
        return {
            "error": str(e),
            "filepath": filepath
        }


# ============================================================================
# RESOURCE: HDF5 BEST PRACTICES
# ============================================================================

@mcp.resource("resource://hdf5/best-practices")
def get_hdf5_best_practices() -> str:
    """Provides HDF5 optimization best practices for scientific computing.

    Returns:
        Markdown guide with compression, chunking, and parallel I/O advice
    """
    return """# HDF5 Best Practices for Scientific Computing

## Compression

- **gzip-6**: Good balance, widely supported
- **blosc**: Best for parallel I/O (10-100x faster decompression)
- **lzf**: Fast, lower compression ratio
- **none**: For temporary files or pre-compressed data

## Chunking

- **Auto**: HDF5 decides (usually good)
- **Manual**: Match access patterns (e.g., (100,100,100) for 3D slices)
- **Rule**: Chunk size should be 100KB - 10MB

## Parallel I/O

- Enable MPI-IO collective writes
- Use independent I/O for small datasets
- Align chunks to filesystem stripe size

## Performance

- Write larger chunks at once (reduce syscalls)
- Use compression for network filesystems
- Disable compression for SSD/local storage
"""


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ClaudIO HDF5 MCP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")

    args = parser.parse_args()

    print(f"Starting HDF5 MCP Server at http://{args.host}:{args.port}")
    print(f"Available tools: hdf5_analyze, hdf5_get_info, hdf5_list_datasets")
    print(f"Resources: resource://hdf5/best-practices")

    mcp.run(transport="sse", host=args.host, port=args.port)
