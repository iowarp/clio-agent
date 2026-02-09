# FastMCP Client Usage
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Comprehensive guide to the FastMCP `Client` class for connecting to MCP servers, calling tools, reading resources, and managing transports.

---

## Table of Contents

1. [Client Basics](#1-client-basics)
2. [Transport Auto-Detection](#2-transport-auto-detection)
3. [Core Operations](#3-core-operations)
4. [Client Handlers](#4-client-handlers)
5. [Multi-Server Configuration](#5-multi-server-configuration)
6. [Proxy & Bridging](#6-proxy--bridging)
7. [Error Handling](#7-error-handling)
8. [Testing with Client](#8-testing-with-client)
9. [CLIO Integration Patterns](#9-clio-integration-patterns)

---

## 1. Client Basics

### Constructor

```python
from fastmcp import Client

client = Client(
    server_or_url: FastMCP | str | dict,  # Server, URL, path, or config
    auto_initialize: bool = True,          # Auto-initialize on connect
    timeout: float | None = None,          # Default timeout (seconds)
    log_handler: Callable | None = None,   # Log message callback
    progress_handler: Callable | None = None,   # Progress notification callback
    sampling_handler: Callable | None = None,   # LLM sampling callback
    elicitation_handler: Callable | None = None, # User input callback
    roots_handler: Callable | None = None,       # Filesystem roots callback (v2.0.0+)
    roots: list[str] | Callable | None = None,   # Static or dynamic roots (v2.0.0+)
)
```

### Context Manager Pattern (Required)

The Client **must** be used as an async context manager:

```python
from fastmcp import Client

async with Client("http://localhost:8000/mcp") as client:
    tools = await client.list_tools()
    result = await client.call_tool("add", {"a": 1, "b": 2})
    print(result)
```

The context manager handles connection setup and teardown. Do not call methods outside the `async with` block.

### Basic Usage

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("my_server.py") as client:
        # List available tools
        tools = await client.list_tools()
        for tool in tools:
            print(f"{tool.name}: {tool.description}")

        # Call a tool
        result = await client.call_tool("search", {"query": "HDF5 schema"})
        print(result)

asyncio.run(main())
```

---

## 2. Transport Auto-Detection

The Client automatically selects the transport based on the input type:

| Input | Transport | Example |
|-------|-----------|---------|
| `FastMCP` instance | In-memory | `Client(my_server)` |
| HTTP/HTTPS URL | HTTP (Streamable) | `Client("http://localhost:8000/mcp")` |
| `.py` file path | STDIO subprocess | `Client("my_server.py")` |
| `dict` config | Multi-server | `Client({"mcpServers": {...}})` |

### In-Memory (Direct Server Instance)

No subprocess or network — fastest option, ideal for testing:

```python
from fastmcp import FastMCP, Client

server = FastMCP("TestServer")

@server.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

async with Client(server) as client:
    result = await client.call_tool("add", {"a": 1, "b": 2})
    assert result.data == 3
```

### HTTP Transport

```python
# Standard HTTP endpoint
async with Client("http://localhost:8000/mcp") as client:
    result = await client.call_tool("query", {"sql": "SELECT * FROM data"})

# HTTPS with custom headers (for auth)
async with Client("https://api.example.com/mcp") as client:
    result = await client.call_tool("analyze", {"file": "data.h5"})
```

### STDIO Transport

Launches a subprocess running the server script:

```python
# Relative path
async with Client("my_server.py") as client:
    tools = await client.list_tools()

# Absolute path
async with Client("/opt/tools/data_server.py") as client:
    result = await client.call_tool("inspect", {"path": "/data/exp.h5"})
```

---

## 3. Core Operations

### Connectivity

```python
async with Client(server) as client:
    # Test connectivity
    await client.ping()
```

### Tools

```python
async with Client(server) as client:
    # List all available tools
    tools = await client.list_tools()
    for tool in tools:
        print(f"{tool.name}: {tool.description}")
        print(f"  Schema: {tool.inputSchema}")

    # Call a tool with arguments
    result = await client.call_tool("add", {"a": 5, "b": 3})
    print(result)       # CallToolResult
    print(result.data)  # Parsed result data (convenience property)
```

### Resources

```python
async with Client(server) as client:
    # List static resources
    resources = await client.list_resources()
    for r in resources:
        print(f"{r.uri}: {r.name}")

    # List resource templates (parameterized)
    templates = await client.list_resource_templates()

    # Read a resource by URI
    contents = await client.read_resource("file:///data/config.yaml")
    for item in contents:
        print(item.text or item.blob)
```

### Prompts

```python
async with Client(server) as client:
    # List available prompts
    prompts = await client.list_prompts()
    for p in prompts:
        print(f"{p.name}: {p.description}")

    # Get a prompt with arguments
    prompt_result = await client.get_prompt("analyze_data", {"filepath": "/data/exp.h5"})
    for msg in prompt_result.messages:
        print(f"{msg.role}: {msg.content}")
```

### Resource Subscriptions (v3.0.0+)

```python
async with Client(server) as client:
    async def on_update(uri: str):
        contents = await client.read_resource(uri)
        print(f"Resource updated: {uri}")

    await client.subscribe_resource("file:///data/config.yaml", on_update)
```

---

## 4. Client Handlers

Handlers enable the client to respond to server-initiated requests during tool execution.

### Log Handler

Receives log messages from the server:

```python
def log_handler(level: str, message: str, logger_name: str | None = None):
    print(f"[{level}] {logger_name or 'server'}: {message}")

client = Client("server.py", log_handler=log_handler)
```

### Progress Handler

Receives progress notifications for long-running operations:

```python
def progress_handler(progress: float, total: float | None, token: str | None):
    if total:
        pct = (progress / total) * 100
        print(f"Progress: {pct:.0f}%")
    else:
        print(f"Progress: {progress}")

client = Client("server.py", progress_handler=progress_handler)
```

### Sampling Handler

Allows the server to request LLM completions from the client:

```python
async def sampling_handler(messages, model_preferences=None, **kwargs):
    # Forward to your LLM
    response = await my_llm.complete(messages=messages)
    return response

client = Client("server.py", sampling_handler=sampling_handler)
```

### Elicitation Handler

Handles server requests for structured user input:

```python
from fastmcp.client.elicitation import ElicitResult, ElicitRequestParams, RequestContext

async def elicitation_handler(
    message: str,
    response_type: type | None,
    params: ElicitRequestParams,
    context: RequestContext,
) -> ElicitResult | object:
    # Show message to user, collect input
    user_input = input(f"{message}: ")

    if response_type:
        return response_type(value=user_input)

    # Or explicit control:
    return ElicitResult(action="accept", content=response_type(value=user_input))
    # ElicitResult(action="decline")  — user declined
    # ElicitResult(action="cancel")   — cancel entire operation

client = Client("server.py", elicitation_handler=elicitation_handler)
```

### Roots Handler

Provides filesystem root boundaries to the server:

```python
from fastmcp.client.roots import RequestContext

# Static roots
client = Client("server.py", roots=["/data/experiments", "/data/configs"])

# Dynamic roots via callback
async def roots_callback(context: RequestContext) -> list[str]:
    return ["/data/experiments", "/data/configs"]

client = Client("server.py", roots=roots_callback)
```

---

## 5. Multi-Server Configuration

Connect to multiple MCP servers through a single client using a configuration dict:

```python
config = {
    "mcpServers": {
        "data_tools": {
            "url": "http://localhost:8001/mcp",
            "transport": "http"
        },
        "io_tools": {
            "url": "http://localhost:8002/mcp",
            "transport": "http"
        },
        "local_tools": {
            "command": "python",
            "args": ["local_server.py"],
        }
    }
}

async with Client(config) as client:
    # Tools are namespaced: data_tools_search, io_tools_read, etc.
    tools = await client.list_tools()
    result = await client.call_tool("data_tools_search", {"query": "experiment"})
```

### Namespacing

Multi-server configurations automatically namespace components:

| Component Type | Pattern |
|----------------|---------|
| Tools | `{server_name}_{tool_name}` |
| Prompts | `{server_name}_{prompt_name}` |
| Resources | `protocol://{server_name}/path` |

---

## 6. Proxy & Bridging

### create_proxy()

Create a proxy server that forwards requests to a backend:

```python
from fastmcp.server import create_proxy

# Bridge HTTP backend to stdio
proxy = create_proxy("http://backend:8000/mcp", name="MyProxy")
proxy.run()  # Runs as stdio (default)

# Bridge stdio to HTTP
proxy = create_proxy("backend_server.py", name="StdioToHTTP")
proxy.run(transport="http", host="0.0.0.0", port=8080)
```

### Session Isolation

By default, each client connection gets an isolated backend session:

```python
# Isolated (recommended) — pass URL/path
proxy = create_proxy("backend_server.py")
# Client A → own session, Client B → own session

# Shared (single-threaded only) — pass connected Client
async with Client("backend_server.py") as connected:
    proxy = create_proxy(connected)
    # All clients share one session (careful with concurrency)
```

### Mounting Proxies

Combine local tools with proxied remote tools:

```python
from fastmcp import FastMCP
from fastmcp.server import create_proxy

server = FastMCP("Combined")

@server.tool
def local_compute(data: str) -> str:
    """Local computation."""
    return process(data)

# Mount remote tools
remote = create_proxy("http://remote:8000/mcp")
server.mount(remote)
# Now has both local_compute and all remote tools
```

### Multi-Server Proxy via Config

```python
config = {
    "mcpServers": {
        "weather": {"url": "https://weather-api.example.com/mcp"},
        "calendar": {"url": "https://calendar-api.example.com/mcp"},
    }
}
proxy = create_proxy(config, name="AggregatedProxy")
# Exposes: weather_get_forecast, calendar_add_event, etc.
```

---

## 7. Error Handling

### Connection Errors

```python
from fastmcp import Client

try:
    async with Client("http://unreachable:8000/mcp") as client:
        await client.ping()
except ConnectionError as e:
    print(f"Cannot connect: {e}")
except TimeoutError as e:
    print(f"Connection timed out: {e}")
```

### Tool Execution Errors

```python
async with Client(server) as client:
    result = await client.call_tool("risky_tool", {"input": "data"})

    # Check for errors in the result
    if result.isError:
        print(f"Tool error: {result.content}")
    else:
        print(f"Success: {result.data}")
```

### Timeout Configuration

```python
# Global timeout
client = Client("server.py", timeout=30.0)  # 30 second default

async with client:
    # Per-operation (if supported)
    result = await client.call_tool("slow_analysis", {"file": "big.h5"})
```

---

## 8. Testing with Client

The in-memory transport makes testing straightforward — no subprocess or network needed.

### Basic Test Pattern

```python
import pytest
from fastmcp import FastMCP, Client

@pytest.fixture
def server():
    mcp = FastMCP("TestServer")

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @mcp.tool
    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    return mcp

@pytest.mark.anyio
async def test_add(server):
    async with Client(server) as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.data == 5

@pytest.mark.anyio
async def test_list_tools(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools]
        assert "add" in names
        assert "multiply" in names
```

### Parametrized Tests

```python
@pytest.mark.anyio
@pytest.mark.parametrize("a, b, expected", [(1, 2, 3), (0, 0, 0), (-1, 1, 0)])
async def test_add_parametrized(server, a, b, expected):
    async with Client(server) as client:
        result = await client.call_tool("add", {"a": a, "b": b})
        assert result.data == expected
```

### Testing with Dependencies

```python
from fastmcp import FastMCP, Client
from fastmcp.server import Context

@pytest.fixture
def server_with_db():
    mcp = FastMCP("DBServer")
    mcp._test_db = {"users": [{"name": "Alice"}]}  # Test data

    @mcp.tool
    def get_users(ctx: Context) -> list:
        """Get all users."""
        return ctx.fastmcp._test_db["users"]

    return mcp

@pytest.mark.anyio
async def test_get_users(server_with_db):
    async with Client(server_with_db) as client:
        result = await client.call_tool("get_users", {})
        assert len(result.data) == 1
        assert result.data[0]["name"] == "Alice"
```

### Snapshot Testing

```python
from inline_snapshot import snapshot

@pytest.mark.anyio
async def test_tool_schema(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        assert tools == snapshot()  # Run with --inline-snapshot=fix,create
```

---

## 9. CLIO Integration Patterns

### DSPy Tool Bridge via Client

```python
import dspy
from fastmcp import Client

async def load_mcp_tools(server_url: str) -> list[dspy.Tool]:
    """Load MCP tools as DSPy tools via Client."""
    async with Client(server_url) as client:
        mcp_tools = await client.list_tools()
        dspy_tools = []
        for tool in mcp_tools:
            dspy_tools.append(dspy.Tool.from_mcp_tool(client, tool))
        return dspy_tools

# Use in CLIO agent
tools = await load_mcp_tools("http://localhost:8000/mcp")
agent = dspy.ReAct("query -> answer", tools=tools)
```

### Health Check Pattern

```python
from fastmcp import Client

async def check_mcp_server(url: str) -> dict:
    """Health check for an MCP server."""
    try:
        async with Client(url, timeout=5.0) as client:
            await client.ping()
            tools = await client.list_tools()
            resources = await client.list_resources()
            return {
                "status": "healthy",
                "tools": len(tools),
                "resources": len(resources),
            }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
```

### Gateway Client Pattern

CLIO's MCP gateway connects to multiple tool servers:

```python
from fastmcp import FastMCP, Client

gateway = FastMCP("CLIO Gateway")

# Register tool servers dynamically
TOOL_SERVERS = {
    "hdf5": "http://localhost:8001/mcp",
    "parquet": "http://localhost:8002/mcp",
    "csv": "http://localhost:8003/mcp",
}

@gateway.tool
async def call_tool_server(server: str, tool_name: str, arguments: dict) -> str:
    """Route a tool call to the appropriate server."""
    if server not in TOOL_SERVERS:
        return f"Unknown server: {server}"

    async with Client(TOOL_SERVERS[server]) as client:
        result = await client.call_tool(tool_name, arguments)
        return str(result.data)
```

---

## Quick Reference

| Operation | Code |
|-----------|------|
| Connect | `async with Client(server) as client:` |
| List tools | `await client.list_tools()` |
| Call tool | `await client.call_tool("name", {"arg": "val"})` |
| Read resource | `await client.read_resource("uri://path")` |
| Get prompt | `await client.get_prompt("name", {"arg": "val"})` |
| Health check | `await client.ping()` |
| Subscribe | `await client.subscribe_resource(uri, callback)` |

**Transport Selection:**
- Testing → `Client(server_instance)` (in-memory)
- Local dev → `Client("server.py")` (stdio)
- Production → `Client("http://host:port/mcp")` (HTTP)
- Multi-server → `Client({"mcpServers": {...}})` (config)

**See also:**
- [00_FASTMCP_API_REFERENCE.md](00_FASTMCP_API_REFERENCE.md) — Full API reference
- [03_COMPOSITION.md](03_COMPOSITION.md) — Server composition and mounting
- [06_TESTING.md](06_TESTING.md) — Testing patterns in depth
- [07_CLIO_PATTERNS.md](07_CLIO_PATTERNS.md) — CLIO-specific integration
