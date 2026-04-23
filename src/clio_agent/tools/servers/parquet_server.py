"""
Parquet MCP Server for CLIO Agent

A FastMCP server providing 3 curated tools for Parquet file analysis
using real pyarrow operations. Each tool is designed for a specific
agent use case (agent story) in the CLIO data analysis workflow.

Tools:
    - analyze_schema: Discover Parquet file structure and metadata
    - query_data: Sample rows and specific columns from Parquet files
    - compute_statistics: Column-level statistical profiling

Usage:
    >>> from clio_agent.tools.servers.parquet_server import parquet_server
    >>> # Use with FastMCP Client for in-memory testing
    >>> from fastmcp import Client
    >>> async with Client(parquet_server) as client:
    ...     result = await client.call_tool("analyze_schema", {"filepath": "data.parquet"})
"""

import os
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from fastmcp import FastMCP

from clio_agent.tools.file_policy import (
    FilePolicyError,
    validate_non_empty_string,
    validate_positive_int,
    validate_read_path,
)

parquet_server = FastMCP("parquet")


@parquet_server.tool()
def analyze_schema(filepath: str) -> dict[str, Any]:
    """Inspect the schema and metadata of a Parquet file: column names, types,
    row count, row groups, file size, and creator metadata.

    Agent story: When a user asks what's in their Parquet file, or when I need
    to discover file structure before running statistics or queries. This is
    always the first tool to reach for with a new Parquet file.

    Args:
        filepath: Path to the Parquet file

    Returns:
        Dictionary with columns (name + type), num_rows, num_row_groups,
        file_size_bytes, and created_by metadata.
    """
    try:
        safe_path = validate_read_path(filepath)
        parquet_file = pq.ParquetFile(safe_path)
        schema = parquet_file.schema_arrow
        metadata = parquet_file.metadata

        columns = []
        for i in range(len(schema)):
            field = schema.field(i)
            columns.append(
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": field.nullable,
                }
            )

        result: dict[str, Any] = {
            "filepath": str(safe_path),
            "num_columns": len(columns),
            "columns": columns,
            "num_rows": metadata.num_rows,
            "num_row_groups": metadata.num_row_groups,
            "file_size_bytes": os.path.getsize(safe_path),
        }

        # Creator metadata (may be None)
        if metadata.created_by:
            result["created_by"] = metadata.created_by

        # Key-value metadata from schema
        if schema.metadata:
            kv_metadata = {
                k.decode("utf-8") if isinstance(k, bytes) else str(k): (
                    v.decode("utf-8") if isinstance(v, bytes) else str(v)
                )
                for k, v in schema.metadata.items()
            }
            result["key_value_metadata"] = kv_metadata

        return result
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@parquet_server.tool()
def query_data(
    filepath: str,
    columns: str = "",
    row_limit: int = 100,
) -> dict[str, Any]:
    """Read rows from a Parquet file, optionally selecting specific columns.

    Agent story: When a user wants to see actual data values, sample specific
    columns, or inspect a subset of rows to understand data content and quality.

    Args:
        filepath: Path to the Parquet file
        columns: Comma-separated column names to select (empty string = all columns)
        row_limit: Maximum number of rows to return (default 100)

    Returns:
        Dictionary with rows as list of dicts, column names, total_rows,
        and rows_returned count.
    """
    try:
        validate_positive_int(row_limit, field="row_limit", max_value=10000)
        safe_path = validate_read_path(filepath)
        # Parse column selection
        col_list = None
        if columns and columns.strip():
            col_list = [c.strip() for c in columns.split(",") if c.strip()]

        table = pq.read_table(safe_path, columns=col_list)
        total_rows = len(table)

        # Limit rows
        if row_limit > 0 and total_rows > row_limit:
            table = table.slice(0, row_limit)

        # Convert to list of dicts
        rows = table.to_pydict()
        # pyarrow to_pydict returns {col: [values]} -- convert to [{col: val}, ...]
        column_names = list(rows.keys())
        num_returned = len(table)
        records = []
        for i in range(num_returned):
            record = {}
            for col in column_names:
                val = rows[col][i]
                # Convert numpy/pyarrow types to Python native for JSON serialization
                if isinstance(val, (np.integer,)):
                    val = int(val)
                elif isinstance(val, (np.floating,)):
                    val = float(val)
                elif isinstance(val, np.ndarray):
                    val = val.tolist()
                record[col] = val
            records.append(record)

        return {
            "filepath": str(safe_path),
            "columns": column_names,
            "total_rows": total_rows,
            "rows_returned": num_returned,
            "rows": records,
        }
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}


@parquet_server.tool()
def compute_statistics(filepath: str, column: str) -> dict[str, Any]:
    """Compute detailed statistics for a single column in a Parquet file.

    For numeric columns: min, max, mean, std, median, null_count, unique_count, dtype.
    For non-numeric columns: unique_count, null_count, top 5 value counts, dtype.

    Agent story: When a user asks for column statistics, distribution info,
    data quality assessment, or when I need to understand the range and spread
    of values before recommending analysis strategies.

    Args:
        filepath: Path to the Parquet file
        column: Name of the column to analyze

    Returns:
        Dictionary with column statistics appropriate to the data type.
    """
    try:
        validate_non_empty_string(column, field="column")
        safe_path = validate_read_path(filepath)
        table = pq.read_table(safe_path, columns=[column])

        if column not in table.column_names:
            return {"error": f"Column '{column}' not found in file"}

        col_array = table.column(column)
        col_type = col_array.type
        null_count = col_array.null_count
        total_count = len(col_array)

        result: dict[str, Any] = {
            "filepath": str(safe_path),
            "column": column,
            "dtype": str(col_type),
            "total_count": total_count,
            "null_count": null_count,
        }

        is_numeric = pa.types.is_integer(col_type) or pa.types.is_floating(col_type)

        if is_numeric:
            non_null = col_array.drop_null()
            if len(non_null) == 0:
                result["unique_count"] = 0
                result["non_null_count"] = 0
                return result

            series = non_null.to_numpy(zero_copy_only=False)
            if np.issubdtype(series.dtype, np.floating):
                valid = series[~np.isnan(series)]
            else:
                valid = series

            if len(valid) == 0:
                result["unique_count"] = 0
                result["non_null_count"] = 0
                return result

            result["min"] = float(np.min(valid))
            result["max"] = float(np.max(valid))
            result["mean"] = float(np.mean(valid))
            result["std"] = float(np.std(valid))
            result["median"] = float(np.median(valid))
            result["unique_count"] = int(len(np.unique(valid)))
            result["non_null_count"] = int(len(valid))
        else:
            # String/categorical statistics
            py_values = col_array.to_pylist()
            non_null_values = [v for v in py_values if v is not None]
            unique_values = set(non_null_values)
            result["unique_count"] = len(unique_values)

            # Top 5 value counts
            if non_null_values:
                value_counts: dict[str, int] = {}
                for v in non_null_values:
                    sv = str(v)
                    value_counts[sv] = value_counts.get(sv, 0) + 1
                # Sort by count descending, take top 5
                sorted_counts = sorted(value_counts.items(), key=lambda x: x[1], reverse=True)
                result["value_counts"] = dict(sorted_counts[:5])

        return result
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}
