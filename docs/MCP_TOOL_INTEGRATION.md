# MCP Tool Integration

How CLIO Agent integrates scientific tool servers using FastMCP and DSPy.

**Version**: 2.0 | **Updated**: 2026-02-09

---

## Gateway Architecture

All MCP servers are composed into a single gateway using FastMCP `mount()`. Experts connect to the gateway, not individual servers.

```python
from fastmcp import FastMCP

# Create gateway
gateway = FastMCP("clio-gateway")

# Mount scientific tool servers with namespacing
gateway.mount("/hdf5", hdf5_server)      # -> hdf5_list_datasets, hdf5_analyze_dataset, ...
gateway.mount("/parquet", parquet_server)  # -> parquet_analyze_schema, ...
gateway.mount("/slurm", slurm_server)     # -> slurm_submit_job, ...

# Gateway-level tools
@gateway.tool()
def analyze_file(filepath: str, format: str | None = None) -> dict:
    """Auto-detect file format and route to appropriate backend."""
    if format is None:
        format = detect_format(filepath)
    return {"filepath": filepath, "format": format, "backend": f"{format}_server"}

@gateway.tool()
def list_capabilities() -> dict:
    """List all available tools across all servers."""
    # Used by main agent for dynamic tool discovery
    ...
```

---

## DSPy Tool Bridge

MCP tools are bridged to DSPy agents natively using `dspy.Tool.from_mcp_tool()`:

```python
from fastmcp import Client
import dspy

async def create_expert_with_tools(gateway):
    async with Client(gateway) as client:
        # Get MCP tool definitions
        mcp_tools = await client.list_tools()

        # Convert to DSPy tools (native bridge, no custom code)
        dspy_tools = [dspy.Tool.from_mcp_tool(t) for t in mcp_tools]

        # Create a blueprint ReAct module with the blueprint's compiled signature.
        expert = dspy.ReAct(
            compiled_blueprint_signature,
            tools=dspy_tools,
            max_iters=5
        )
        return expert
```

This replaces the 789-line `mcp_connector.py` (custom async/sync bridge, thread pool, event loop management) with ~5 lines of native DSPy/FastMCP code.

---

## Building MCP Servers

Each scientific data format gets a dedicated FastMCP server.

### HDF5 Server Example

```python
from fastmcp import FastMCP
import h5py
import numpy as np

def create_hdf5_server() -> FastMCP:
    mcp = FastMCP("hdf5-analysis")

    @mcp.tool()
    def list_datasets(filepath: str) -> list[str]:
        """List all datasets in an HDF5 file.

        Agent Story: Use when the user mentions an HDF5 file and you need
        to understand its structure before recommending optimizations.
        """
        datasets = []
        with h5py.File(filepath, "r") as f:
            f.visititems(lambda name, obj: datasets.append(name)
                         if isinstance(obj, h5py.Dataset) else None)
        return datasets

    @mcp.tool()
    def analyze_dataset(filepath: str, dataset_name: str) -> dict:
        """Analyze an HDF5 dataset's shape, dtype, compression, and statistics.

        Agent Story: Use after list_datasets to get details about a specific
        dataset. Returns everything needed to recommend optimization.
        """
        with h5py.File(filepath, "r") as f:
            ds = f[dataset_name]
            stats = {
                "shape": list(ds.shape),
                "dtype": str(ds.dtype),
                "size_bytes": ds.nbytes,
                "compression": ds.compression or "none",
                "chunks": ds.chunks,
            }
            if np.issubdtype(ds.dtype, np.number):
                data = ds[:]
                stats.update({"mean": float(np.mean(data)),
                              "std": float(np.std(data))})
            return stats

    @mcp.tool()
    def optimize_chunking(filepath: str, dataset_name: str,
                          access_pattern: str = "sequential") -> dict:
        """Recommend optimal chunk size and compression for a dataset.

        Agent Story: Use when you need to suggest specific chunking
        improvements. Requires knowing the access pattern.
        """
        with h5py.File(filepath, "r") as f:
            ds = f[dataset_name]
            shape = ds.shape
            if access_pattern == "sequential":
                chunks = (min(shape[0], 1000),) + shape[1:]
            elif access_pattern == "random":
                chunks = tuple(min(s, 100) for s in shape)
            else:
                chunks = tuple(min(s, 500) for s in shape)
            return {
                "current_chunks": ds.chunks,
                "suggested_chunks": chunks,
                "current_compression": ds.compression,
                "suggested_compression": "gzip",
                "compression_level": 6,
            }

    @mcp.tool()
    def check_compression(filepath: str) -> dict:
        """Check compression ratio and suggest improvements for all datasets.

        Agent Story: Use for a quick overview of compression across the
        entire file. Good first step before detailed analysis.
        """
        results = {}
        with h5py.File(filepath, "r") as f:
            def check(name, obj):
                if isinstance(obj, h5py.Dataset):
                    results[name] = {
                        "compression": obj.compression or "none",
                        "size_bytes": obj.nbytes,
                    }
            f.visititems(check)
        return results

    return mcp
```

**Note**: 4 tools, not 10. Each tool is high-level and self-contained. Agent stories explain when to use each.

---

## Tool Curation Principles

From industry research (OpenAI data agent, Anthropic's agent guide, Google ADK):

### 1. Max 5-7 Tools Per Expert

Too many tools confuse the LM. Instead of exposing every operation:

```
BAD (12 tools):
  get_shape, get_dtype, get_compression, get_chunks, get_attrs,
  read_slice, write_data, delete_dataset, rename_dataset,
  copy_dataset, compress_dataset, rechunk_dataset

GOOD (4 tools):
  list_datasets, analyze_dataset, optimize_chunking, check_compression
```

### 2. Composite Over Atomic

One tool that does a complete operation is better than many tiny tools:

```python
# BAD: Agent must call 3 tools and combine results
shape = get_shape(filepath, dataset)
dtype = get_dtype(filepath, dataset)
compression = get_compression(filepath, dataset)

# GOOD: One tool returns everything
analysis = analyze_dataset(filepath, dataset)
# Returns: shape, dtype, compression, stats
```

### 3. Agent Story Documentation

Every tool docstring includes an "Agent Story" explaining when/why to use it:

```python
@mcp.tool()
def analyze_dataset(filepath: str, dataset_name: str) -> dict:
    """Analyze an HDF5 dataset's statistics.

    Agent Story: Use after list_datasets to get details about a specific
    dataset. Returns everything needed to recommend optimization.
    Call this BEFORE optimize_chunking to understand current state.
    """
```

### 4. Dynamic Discovery

Main agent doesn't load all tool schemas at startup. Instead:

```python
# At startup: only load gateway's list_capabilities tool (~400 tokens)
# When routing to DataExpert: load HDF5 tools (~2K tokens)
# When routing to HPCExpert: load SLURM tools (~2K tokens)
# Result: context overhead drops from ~47K to ~400-2K tokens
```

---

## ARC Caching Integration

Tool results are cached in ARC with configurable TTL:

```python
from fastmcp import FastMCP, Depends

def get_arc():
    return ARCMemory()

@mcp.tool()
def analyze_dataset(filepath: str, dataset_name: str,
                    arc: ARCMemory = Depends(get_arc)) -> dict:
    """Analyze dataset with ARC caching."""
    # Check cache first
    cache_key = f"hdf5:analyze:{filepath}:{dataset_name}"
    cached = arc.get_cached_tool_result("hdf5", "analyze", {
        "filepath": filepath, "dataset_name": dataset_name
    })
    if cached:
        return cached

    # Execute analysis
    result = _do_analysis(filepath, dataset_name)

    # Cache for 1 hour
    arc.cache_tool_result("hdf5", "analyze", {
        "filepath": filepath, "dataset_name": dataset_name
    }, result, ttl_seconds=3600)

    return result
```

The `Depends(get_arc)` parameter is hidden from the LLM tool schema (it sees only `filepath` and `dataset_name`).

---

## Testing MCP Servers

### In-Memory Testing (No Subprocess)

```python
import pytest
from fastmcp import Client

@pytest.mark.asyncio
async def test_hdf5_list_datasets(tmp_path):
    # Create test HDF5 file
    import h5py
    filepath = str(tmp_path / "test.h5")
    with h5py.File(filepath, "w") as f:
        f.create_dataset("temperature", data=[1.0, 2.0, 3.0])
        f.create_dataset("pressure", data=[100, 200, 300])

    # Test server in-memory (no subprocess, no network)
    server = create_hdf5_server()
    async with Client(server) as client:
        result = await client.call_tool("list_datasets", {"filepath": filepath})
        assert "temperature" in result
        assert "pressure" in result

@pytest.mark.asyncio
async def test_hdf5_analyze_dataset(tmp_path):
    filepath = str(tmp_path / "test.h5")
    with h5py.File(filepath, "w") as f:
        f.create_dataset("data", data=np.random.rand(100, 100), compression="gzip")

    server = create_hdf5_server()
    async with Client(server) as client:
        result = await client.call_tool("analyze_dataset", {
            "filepath": filepath,
            "dataset_name": "data"
        })
        assert result["compression"] == "gzip"
        assert result["shape"] == [100, 100]
```

### Gateway Integration Testing

```python
@pytest.mark.asyncio
async def test_gateway_routing(tmp_path):
    # Create gateway with mounted servers
    gateway = create_gateway()

    async with Client(gateway) as client:
        # Test namespaced tool access
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        assert "hdf5_list_datasets" in tool_names
        assert "analyze_file" in tool_names  # Gateway-level tool
```

---

## Transforms for Access Control

FastMCP transforms control which tools are exposed:

```python
from fastmcp.transforms import Enabled, Namespace

# Only expose read-only tools to unprivileged users
gateway.mount("/hdf5", hdf5_server, transforms=[
    Enabled(lambda tool: "optimize" not in tool.name)  # Read-only mode
])

# Full access for admin users
gateway.mount("/hdf5-admin", hdf5_server)  # All tools
```

---

## Server Deployment

### Development (In-Process)

```python
# Server runs in same process as agent (fastest for dev)
server = create_hdf5_server()
gateway.mount("/hdf5", server)
```

### Production (Streamable HTTP)

```python
# Server runs as standalone HTTP service
from fastmcp import FastMCP

server = create_hdf5_server()
server.run(transport="http", host="0.0.0.0", port=8001)

# Gateway connects via HTTP
gateway.mount("/hdf5", "http://hdf5-server:8001/mcp")
```

### HPC (stdio via uvx)

```bash
# Server runs via uvx launcher
uvx iowarp-mcps hdf5

# Gateway connects via stdio
gateway.mount("/hdf5", {"command": "uvx", "args": ["iowarp-mcps", "hdf5"]})
```

---

## Related Documentation

- [CLIO_AGENT_ARCHITECTURE.md](CLIO_AGENT_ARCHITECTURE.md) - System architecture
- [DSPY_BLUEPRINT_EXPERT_RUNTIME.md](DSPY_BLUEPRINT_EXPERT_RUNTIME.md) - Expert agent patterns
- [ai-docs/FASTMCP/](../ai-docs/FASTMCP/) - FastMCP reference material
- [ai-docs/DSPY/06_ADVANCED_PATTERNS.md](../ai-docs/DSPY/06_ADVANCED_PATTERNS.md) - DSPy tool integration
