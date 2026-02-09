# FastMCP Server Composition
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Server composition allows you to combine multiple FastMCP servers into unified gateways, enabling modular architecture and capability aggregation.

## Table of Contents
- [Core Concepts](#core-concepts)
- [mount() - Live Server Mounting](#mount---live-server-mounting)
- [import_server() - Static Import](#import_server---static-import)
- [Namespace Prefixing](#namespace-prefixing)
- [Proxy Patterns](#proxy-patterns)
- [Gateway Pattern](#gateway-pattern)
- [Composition vs Import Tradeoffs](#composition-vs-import-tradeoffs)
- [CLIO-Specific: Scientific Tool Gateways](#clio-specific-scientific-tool-gateways)

## Core Concepts

FastMCP provides two primary composition methods:

1. **mount()** - Creates a live link to another server. Changes to the mounted server are reflected dynamically.
2. **import_server()** - Performs a one-time copy of tools, resources, and prompts at initialization.

Both methods support optional namespace prefixing to avoid name collisions.

## mount() - Live Server Mounting

Mounting creates a dynamic link between servers. Updates to the mounted server are automatically available.

### Basic Mounting

```python
from fastmcp import FastMCP

# Create servers
weather_server = FastMCP(name="Weather Service")
analysis_server = FastMCP(name="Analysis Service")

@weather_server.tool()
def get_temperature(city: str) -> float:
    """Get current temperature for a city."""
    return 72.5  # Simplified example

@analysis_server.tool()
def analyze_data(values: list[float]) -> dict:
    """Analyze numerical data."""
    return {
        "mean": sum(values) / len(values),
        "count": len(values)
    }

# Create gateway server
gateway = FastMCP(name="Gateway")

# Mount sub-servers
gateway.mount(weather_server)
gateway.mount(analysis_server)

# Gateway now exposes tools from both servers
if __name__ == "__main__":
    gateway.run()
```

### Mounting with Prefix

```python
from fastmcp import FastMCP

weather_server = FastMCP(name="Weather")
forecasting_server = FastMCP(name="Forecasting")

@weather_server.tool()
def get_data(location: str) -> dict:
    """Get current weather data."""
    return {"temp": 72.5, "humidity": 65}

@forecasting_server.tool()
def get_data(location: str, days: int = 7) -> list:
    """Get weather forecast."""
    return [{"day": i, "temp": 70 + i} for i in range(days)]

# Create gateway with prefixed tools
gateway = FastMCP(name="Weather Gateway")

# Mount with prefixes to avoid name collision
gateway.mount(weather_server, prefix="current")
gateway.mount(forecasting_server, prefix="forecast")

# Tools are now available as:
# - current_get_data(location)
# - forecast_get_data(location, days)
```

### Mounting Remote Servers via Proxy

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

# Create gateway
gateway = FastMCP(name="Multi-Service Gateway")

# Mount local server
local_server = FastMCP(name="Local")

@local_server.tool()
def local_operation(data: str) -> str:
    return f"Processed locally: {data}"

gateway.mount(local_server, prefix="local")

# Mount remote MCP server via HTTP proxy
remote_proxy = create_proxy("http://api.example.com/mcp")
gateway.mount(remote_proxy, prefix="remote")

# Mount another server via stdio
stdio_proxy = create_proxy("./path/to/server.py")
gateway.mount(stdio_proxy, prefix="external")

if __name__ == "__main__":
    gateway.run()
```

## import_server() - Static Import

`import_server()` performs a one-time copy of capabilities at initialization. Changes to the source server after import are not reflected.

### Basic Import

```python
from fastmcp import FastMCP

# Source servers
static_server = FastMCP(name="Static Tools")

@static_server.tool()
def calculate(x: float, y: float) -> float:
    """Perform calculation."""
    return x + y

@static_server.resource("config://{name}")
def get_config(name: str) -> str:
    """Get configuration value."""
    return f"config_{name}"

# Create main server and import
main_server = FastMCP(name="Main")

# One-time import - future changes to static_server won't affect main_server
main_server.import_server(static_server)

# Tools and resources are now part of main_server
```

### Import with Namespace

```python
from fastmcp import FastMCP

utils_server = FastMCP(name="Utilities")

@utils_server.tool()
def format_data(data: dict) -> str:
    """Format data for display."""
    return str(data)

@utils_server.tool()
def validate_input(value: str) -> bool:
    """Validate input string."""
    return len(value) > 0

# Import with namespace prefix
main_server = FastMCP(name="Main")
main_server.import_server(utils_server, namespace="utils")

# Tools are now:
# - utils_format_data(data)
# - utils_validate_input(value)
```

## Namespace Prefixing

Both `mount()` and `import_server()` support namespace prefixing for tools, resources, and prompts.

### Tool Prefixing

```python
from fastmcp import FastMCP

server_a = FastMCP(name="Server A")
server_b = FastMCP(name="Server B")

@server_a.tool()
def process(data: str) -> str:
    return f"A: {data}"

@server_b.tool()
def process(data: str) -> str:
    return f"B: {data}"

gateway = FastMCP(name="Gateway")

# Without prefix, second tool would overwrite first
gateway.mount(server_a, prefix="a")
gateway.mount(server_b, prefix="b")

# Now accessible as a_process() and b_process()
```

### Resource Prefixing

```python
from fastmcp import FastMCP

data_server = FastMCP(name="Data Provider")

@data_server.resource("data://{id}")
def get_data(id: str) -> str:
    return f"Data for {id}"

gateway = FastMCP(name="Gateway")
gateway.mount(data_server, prefix="provider")

# Resource URI becomes: provider_data://{id}
```

### Prefix Separator Configuration

```python
from fastmcp import FastMCP

server = FastMCP(name="Sub Server")

@server.tool()
def my_tool() -> str:
    return "result"

gateway = FastMCP(name="Gateway")

# Default separator is underscore
gateway.mount(server, prefix="sub")  # Creates sub_my_tool

# Custom separator can be used via transform (see TRANSFORMS.md)
```

## Proxy Patterns

Proxies enable mounting of remote or external MCP servers.

### HTTP Proxy

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

gateway = FastMCP(name="Gateway")

# Connect to remote MCP server over HTTP
remote_server = create_proxy(
    "http://mcp-server.example.com:8000/mcp",
    transport="http"
)

gateway.mount(remote_server, prefix="remote")

if __name__ == "__main__":
    gateway.run()
```

### Stdio Proxy

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

gateway = FastMCP(name="Gateway")

# Execute and connect to MCP server via stdio
python_server = create_proxy(
    "./my_server.py",
    transport="stdio"
)

gateway.mount(python_server, prefix="python")

# Connect to Node.js MCP server
node_server = create_proxy(
    "node",
    args=["./server.js"],
    transport="stdio"
)

gateway.mount(node_server, prefix="node")
```

### SSE Proxy

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

gateway = FastMCP(name="Gateway")

# Connect to MCP server via Server-Sent Events
sse_server = create_proxy(
    "http://mcp-server.example.com/sse",
    transport="sse"
)

gateway.mount(sse_server, prefix="sse")
```

## Gateway Pattern

The gateway pattern aggregates multiple backend servers into a single MCP endpoint.

### Multi-Backend Gateway

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

# Create gateway server
gateway = FastMCP(
    name="Scientific Gateway",
    version="1.0.0"
)

# Local computation tools
compute_server = FastMCP(name="Compute")

@compute_server.tool()
def calculate_statistics(data: list[float]) -> dict:
    """Calculate descriptive statistics."""
    import statistics
    return {
        "mean": statistics.mean(data),
        "median": statistics.median(data),
        "stdev": statistics.stdev(data) if len(data) > 1 else 0.0
    }

gateway.mount(compute_server, prefix="compute")

# Remote data services
data_proxy = create_proxy("http://data-api.example.com/mcp")
gateway.mount(data_proxy, prefix="data")

# External analysis tools
analysis_proxy = create_proxy("./analysis_server.py")
gateway.mount(analysis_proxy, prefix="analysis")

# Visualization service
viz_proxy = create_proxy("http://viz-service.example.com/mcp")
gateway.mount(viz_proxy, prefix="viz")

if __name__ == "__main__":
    # Single endpoint exposes all capabilities
    gateway.run()
```

### Dynamic Gateway with Authentication

```python
from fastmcp import FastMCP, Context
from fastmcp.proxy import create_proxy
from fastmcp.dependencies import Depends

def get_user_role(ctx: Context) -> str:
    """Extract user role from context."""
    # In production, parse from ctx.client_id or custom headers
    return "admin"  # Simplified

def create_authenticated_gateway() -> FastMCP:
    gateway = FastMCP(name="Authenticated Gateway")

    # Admin-only server
    admin_server = FastMCP(name="Admin")

    @admin_server.tool()
    async def admin_operation(
        action: str,
        role: str = Depends(get_user_role)
    ) -> str:
        """Perform admin operation."""
        if role != "admin":
            raise PermissionError("Admin access required")
        return f"Executed: {action}"

    gateway.mount(admin_server, prefix="admin")

    # Public server
    public_server = FastMCP(name="Public")

    @public_server.tool()
    def public_operation(query: str) -> str:
        """Public operation."""
        return f"Result for: {query}"

    gateway.mount(public_server, prefix="public")

    return gateway

if __name__ == "__main__":
    gateway = create_authenticated_gateway()
    gateway.run()
```

## Composition vs Import Tradeoffs

| Aspect | mount() | import_server() |
|--------|---------|-----------------|
| **Update Propagation** | Live - changes reflected | Static - frozen at import |
| **Memory** | Reference to server | Copies all capabilities |
| **Performance** | Slight routing overhead | Direct access |
| **Use Case** | Dynamic services, development | Stable libraries, production |
| **Debugging** | Can update server independently | Must restart to update |

### When to Use mount()

```python
from fastmcp import FastMCP

# Development scenario - server being actively developed
gateway = FastMCP(name="Dev Gateway")

# Mount development server - changes reflected without restart
dev_server = FastMCP(name="Under Development")
gateway.mount(dev_server)  # Use mount for live updates

# Plugin scenario - external server may update
external_plugin = create_proxy("./plugin.py")
gateway.mount(external_plugin)  # Use mount for dynamic plugins
```

### When to Use import_server()

```python
from fastmcp import FastMCP

# Production scenario - stable utility library
gateway = FastMCP(name="Production Gateway")

# Import stable utilities - won't change
utils_lib = FastMCP(name="Utils Library v1.0")

@utils_lib.tool()
def stable_operation(x: int) -> int:
    return x * 2

gateway.import_server(utils_lib)  # Use import for stable code
```

## CLIO-Specific: Scientific Tool Gateways

CLIO Agent uses composition to create modular scientific tool servers.

### Scientific Data Gateway

```python
from fastmcp import FastMCP
from fastmcp.proxy import create_proxy

def create_scientific_gateway() -> FastMCP:
    """Create CLIO's scientific tool gateway."""

    gateway = FastMCP(
        name="CLIO Scientific Gateway",
        version="0.2.0"
    )

    # HDF5 tools server (local)
    hdf5_server = FastMCP(name="HDF5 Tools")

    @hdf5_server.tool()
    async def read_hdf5_dataset(
        file_path: str,
        dataset_path: str,
        ctx: Context
    ) -> dict:
        """Read dataset from HDF5 file."""
        import h5py
        await ctx.info(f"Reading {dataset_path} from {file_path}")

        with h5py.File(file_path, "r") as f:
            data = f[dataset_path][:]
            return {
                "shape": list(data.shape),
                "dtype": str(data.dtype),
                "data": data.tolist()[:100]  # First 100 elements
            }

    gateway.mount(hdf5_server, prefix="hdf5")

    # Parquet tools server (local)
    parquet_server = FastMCP(name="Parquet Tools")

    @parquet_server.tool()
    async def read_parquet_metadata(
        file_path: str,
        ctx: Context
    ) -> dict:
        """Read Parquet file metadata."""
        import pyarrow.parquet as pq
        await ctx.info(f"Reading metadata from {file_path}")

        parquet_file = pq.ParquetFile(file_path)
        return {
            "num_rows": parquet_file.metadata.num_rows,
            "num_columns": parquet_file.metadata.num_columns,
            "schema": str(parquet_file.schema)
        }

    gateway.mount(parquet_server, prefix="parquet")

    # IOWarp data lake (remote or local)
    # In production, might be remote service
    iowarp_server = FastMCP(name="IOWarp")

    @iowarp_server.tool()
    async def query_iowarp_index(
        query: str,
        ctx: Context
    ) -> list[dict]:
        """Query IOWarp data index."""
        await ctx.info(f"Querying IOWarp: {query}")
        # Integration with IOWarp service
        return []

    gateway.mount(iowarp_server, prefix="iowarp")

    return gateway

if __name__ == "__main__":
    gateway = create_scientific_gateway()
    gateway.run()
```

### Domain-Specific Server Composition

```python
from fastmcp import FastMCP

def create_domain_expert_gateway(domain: str) -> FastMCP:
    """Create domain-specific tool gateway for CLIO experts."""

    gateway = FastMCP(name=f"CLIO {domain.title()} Expert Gateway")

    # Mount domain-specific servers based on domain
    if domain == "data":
        # Data format servers
        gateway.mount(create_hdf5_server(), prefix="hdf5")
        gateway.mount(create_parquet_server(), prefix="parquet")
        gateway.mount(create_netcdf_server(), prefix="netcdf")

    elif domain == "visualization":
        # Plotting servers
        gateway.mount(create_matplotlib_server(), prefix="mpl")
        gateway.mount(create_plotly_server(), prefix="plotly")

    elif domain == "computation":
        # Numerical computation servers
        gateway.mount(create_numpy_server(), prefix="numpy")
        gateway.mount(create_scipy_server(), prefix="scipy")

    return gateway

# CLIO can dynamically compose gateways based on task requirements
data_expert_tools = create_domain_expert_gateway("data")
```

### Capability Filtering via Composition

```python
from fastmcp import FastMCP

def create_filtered_gateway(
    available_servers: dict[str, FastMCP],
    required_capabilities: list[str]
) -> FastMCP:
    """Create gateway with only required capabilities."""

    gateway = FastMCP(name="Filtered Gateway")

    # Only mount servers needed for current task
    for capability in required_capabilities:
        if capability in available_servers:
            gateway.mount(
                available_servers[capability],
                prefix=capability
            )

    return gateway

# CLIO usage example
all_servers = {
    "hdf5": hdf5_server,
    "parquet": parquet_server,
    "netcdf": netcdf_server,
}

# For a task requiring only HDF5 and Parquet
task_gateway = create_filtered_gateway(
    all_servers,
    required_capabilities=["hdf5", "parquet"]
)
```

## Best Practices

1. **Use Prefixes**: Always use prefixes when mounting multiple servers to avoid name collisions.

2. **Mount for Development**: Use `mount()` during development for live updates without restart.

3. **Import for Production**: Use `import_server()` for stable, unchanging capabilities in production.

4. **Organize by Domain**: Group related tools into domain-specific servers, then compose into gateways.

5. **Lazy Loading**: Create servers lazily and mount only when needed to reduce startup time.

6. **Error Isolation**: Mounted servers can fail independently without crashing the gateway.

7. **Documentation**: Document the composition structure so users understand available capabilities and their prefixes.

## Summary

Server composition in FastMCP enables:
- Modular architecture with clear separation of concerns
- Dynamic capability aggregation via gateways
- Namespace isolation to prevent conflicts
- Remote service integration via proxies
- Flexible deployment topologies

CLIO leverages composition to create specialized tool servers for different scientific domains, enabling the 3-tier agent hierarchy to access the right tools at the right time.
