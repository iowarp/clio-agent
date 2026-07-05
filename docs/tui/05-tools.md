# 05 — Tools

> Every tool CLIO can invoke today. All exposed via the FastMCP gateway at `src/clio_agent/tools/`.

## Gateway layout

`tools/gateway.py` mounts named tool servers under namespaced prefixes (FastMCP 3.x):

```python
gateway = FastMCP("clio-gateway")
_mount_with_namespace(gateway, hdf5_server, "hdf5")
_mount_with_namespace(gateway, parquet_server, "parquet")
# future: slurm_server, plot_server, etc.
```

**Tool count philosophy** (`CLAUDE.md` Rule 5, `docs/MCP_TOOL_INTEGRATION.md`): max 5–7 tools per expert. Current total **8 tools**: 5 HDF5 + 3 Parquet.

## HDF5 tools

Source: `tools/servers/hdf5_server.py` (L84–431). All tools validate paths via `validate_read_path()` which enforces:

- `CLIO_ALLOWED_ROOTS` (colon-separated allowed paths, default `$(pwd)` + `/tmp`)
- `CLIO_MAX_FILE_SIZE_BYTES` (default 1 GB)
- `CLIO_ALLOW_SYMLINKS` (default false)

| Tool | Input | Output | When used |
|---|---|---|---|
| `hdf5_list_datasets` | `filepath: str` | `{total_datasets, datasets: [{path, shape, dtype, size_bytes}]}` | Discover what's inside a file |
| `hdf5_analyze_dataset` | `filepath: str, dataset: str` | `{shape, dtype, compression, chunks, statistics}` | Deep-dive one dataset |
| `hdf5_check_compression` | `filepath: str` | Per-dataset compression + overall ratio | Compression audit |
| `hdf5_optimize_chunking` | `filepath: str, dataset: str, access_pattern: "row"\|"column"\|"random"` | `{current_chunks, recommended_chunks, rationale}` | Chunk recommendation |
| `hdf5_analyze_file` | `filepath: str` | `{datasets, groups, compression_summary}` | First-look overview |

## Parquet tools

Source: `tools/servers/parquet_server.py` (L39–248). Same file-policy guards apply.

| Tool | Input | Output |
|---|---|---|
| `parquet_analyze_schema` | `filepath: str` | `{schema, column_names, column_types, num_rows, num_row_groups, file_size_bytes}` |
| `parquet_query_data` | `filepath: str, columns: str (csv, optional), row_limit: int (1–10000, default 100)` | `{rows: [dict], column_names, total_rows, rows_returned}` |
| `parquet_compute_statistics` | `filepath: str, column: str` | Numeric: `{min, max, mean, std, median, unique_count, null_count}`; non-numeric: `{unique_count, value_counts (top 5), null_count, dtype}` |

## Tool error shape

Tools **return** error dicts rather than raising (`test_tools/test_hdf5_server.py:37-120`). Shape:

```python
{
  "error": {
    "type": "file_policy" | "io_error" | "validation" | "internal",
    "code": "outside_allowed_roots" | "file_not_found" | ...,
    "message": "...",
    "details": {...}
  }
}
```

TUI can treat presence of `"error"` key in a tool_result as a structured failure, paint it red, and keep the turn alive.

## MCP client/server duality

CLIO is **both**:

- **MCP client** — Experts consume tools via `MCPToolBridge` (`tools/execution.py:62-229`) which wraps `fastmcp.Client(gateway)` with a background daemon loop. Setup timeout 10 s, tool timeout 30 s. Converts async MCP tools to sync `dspy.Tool` objects for the ReAct agents.
- **MCP server** — the `gateway` object can be served as a standalone MCP endpoint:
  - in-memory (`async with Client(gateway)`) — used in tests.
  - stdio subprocess.
  - HTTP: `gateway.http_app()` wrapped in Uvicorn (not exposed by the `clio-agent-gact` front door by default).

**For the TUI:** if CLIO ever exposes its MCP over HTTP/SSE, the TUI could drive tools directly without going through the expert-dispatch layer — but today it's internal.

## Tool-call observability

When an expert's ReAct loop calls a tool, the result is persisted to `Invocation.tools_called` (see `04-arc-memory.md`):

```python
ToolCall = {tool: str, params: dict, result: Any, duration_ms: float, cached: bool}
```

`cached` reflects ARC cache hit. The TUI should inspect this list (via invocations API) to render the per-tool gutter and cached/fresh glyphs.

## Future tools (roadmap)

- **SLURM server** (`slurm_list_jobs`, `slurm_submit_job`, guarded) — Phase 4
- **Darshan I/O trace analyser** — Phase 4
- **Parquet extensions** (`parquet_sample_data`, etc.) — Phase 2
- **Plot tools as MCP** (currently experts' matplotlib routines) — Phase 3

Tracked in `../../PLAN.md` + `docs/CLIO_AGENT_ARCHITECTURE.md:675-689`.
