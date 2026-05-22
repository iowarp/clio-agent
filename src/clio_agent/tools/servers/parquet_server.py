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

import math
import os
import random
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
PARQUET_STATS_BATCH_SIZE = 65_536
PARQUET_STATS_SAMPLE_SIZE = 10_000
PARQUET_STATS_UNIQUE_LIMIT = 100_000


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
    columns: str | list[str] = "",
    row_limit: int = 100,
) -> dict[str, Any]:
    """Read rows from a Parquet file, optionally selecting specific columns.

    Agent story: When a user wants to see actual data values, sample specific
    columns, or inspect a subset of rows to understand data content and quality.

    Args:
        filepath: Path to the Parquet file
        columns: Comma-separated column names, or a list of column names, to select
            (empty string = all columns)
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
        if isinstance(columns, list):
            col_list = [str(c).strip() for c in columns if str(c).strip()]
        elif columns and columns.strip():
            col_list = [c.strip() for c in columns.split(",") if c.strip()]
        if col_list == []:
            col_list = None

        parquet_file = pq.ParquetFile(safe_path)
        total_rows = parquet_file.metadata.num_rows
        batch_iter = parquet_file.iter_batches(
            batch_size=row_limit,
            columns=col_list,
        )
        first_batch = next(batch_iter, None)
        if first_batch is None:
            schema = parquet_file.schema_arrow
            if col_list is not None:
                schema = pa.schema([schema.field(name) for name in col_list])
            table = schema.empty_table()
        else:
            table = pa.Table.from_batches([first_batch])

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
        parquet_file = pq.ParquetFile(safe_path)
        schema = parquet_file.schema_arrow

        if schema.get_field_index(column) < 0:
            return {"error": f"Column '{column}' not found in file"}

        col_type = schema.field(column).type
        total_count = parquet_file.metadata.num_rows
        null_count = 0

        result: dict[str, Any] = {
            "filepath": str(safe_path),
            "column": column,
            "dtype": str(col_type),
            "total_count": total_count,
            "null_count": null_count,
        }

        is_numeric = pa.types.is_integer(col_type) or pa.types.is_floating(col_type)

        if is_numeric:
            count = 0
            total = 0.0
            total_sq = 0.0
            min_value: float | None = None
            max_value: float | None = None
            sample: list[float] = []
            unique_values: set[float | int] = set()
            unique_count_capped = False
            rng = random.Random(0)

            for batch in parquet_file.iter_batches(
                batch_size=PARQUET_STATS_BATCH_SIZE,
                columns=[column],
            ):
                arr = batch.column(0)
                null_count += arr.null_count
                non_null = arr.drop_null()
                if len(non_null) == 0:
                    continue
                series = non_null.to_numpy(zero_copy_only=False)
                if np.issubdtype(series.dtype, np.floating):
                    valid = series[~np.isnan(series)]
                else:
                    valid = series
                if len(valid) == 0:
                    continue

                values = valid.astype(np.float64, copy=False)
                batch_min = float(np.min(values))
                batch_max = float(np.max(values))
                min_value = batch_min if min_value is None else min(min_value, batch_min)
                max_value = batch_max if max_value is None else max(max_value, batch_max)
                total += float(np.sum(values, dtype=np.float64))
                total_sq += float(np.sum(values * values, dtype=np.float64))

                for raw_value in values:
                    value = float(raw_value)
                    count += 1
                    if len(sample) < PARQUET_STATS_SAMPLE_SIZE:
                        sample.append(value)
                    else:
                        idx = rng.randrange(count)
                        if idx < PARQUET_STATS_SAMPLE_SIZE:
                            sample[idx] = value
                    if not unique_count_capped:
                        unique_values.add(value)
                        if len(unique_values) > PARQUET_STATS_UNIQUE_LIMIT:
                            unique_count_capped = True

            result["null_count"] = null_count
            if count == 0:
                result["unique_count"] = 0
                result["non_null_count"] = 0
                return result

            mean = total / count
            variance = max((total_sq / count) - (mean * mean), 0.0)
            result["min"] = min_value
            result["max"] = max_value
            result["mean"] = mean
            result["std"] = math.sqrt(variance)
            result["median"] = float(np.median(np.array(sample, dtype=np.float64)))
            result["median_approximate"] = count > PARQUET_STATS_SAMPLE_SIZE
            result["unique_count"] = len(unique_values)
            result["unique_count_capped"] = unique_count_capped
            result["non_null_count"] = count
        else:
            value_counts: dict[str, int] = {}
            string_unique_values: set[str] = set()
            unique_count_capped = False
            value_counts_capped = False

            for batch in parquet_file.iter_batches(
                batch_size=PARQUET_STATS_BATCH_SIZE,
                columns=[column],
            ):
                arr = batch.column(0)
                null_count += arr.null_count
                for value in arr.to_pylist():
                    if value is None:
                        continue
                    sv = str(value)
                    if not unique_count_capped:
                        string_unique_values.add(sv)
                        if len(string_unique_values) > PARQUET_STATS_UNIQUE_LIMIT:
                            unique_count_capped = True
                    if sv not in value_counts and len(value_counts) >= PARQUET_STATS_UNIQUE_LIMIT:
                        value_counts_capped = True
                        continue
                    value_counts[sv] = value_counts.get(sv, 0) + 1

            result["null_count"] = null_count
            result["unique_count"] = len(string_unique_values)
            result["unique_count_capped"] = unique_count_capped
            if value_counts:
                sorted_counts = sorted(value_counts.items(), key=lambda x: x[1], reverse=True)
                result["value_counts"] = dict(sorted_counts[:5])
                result["value_counts_capped"] = value_counts_capped

        return result
    except FilePolicyError as e:
        return e.to_result()
    except Exception as e:
        return {"error": str(e)}
