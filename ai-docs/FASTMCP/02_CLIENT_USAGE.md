# FastMCP Client Usage
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Comprehensive guide to the FastMCP `Client` class for connecting to MCP servers, calling tools, reading resources, and managing transports.

---

## Table of Contents

1. [Client Basics](#1-client-basics)
2. [Transport System](#2-transport-system)
3. [Core Operations](#3-core-operations)
4. [Client Handlers](#4-client-handlers)
5. [Authentication](#5-authentication)
6. [Multi-Server Configuration](#6-multi-server-configuration)
7. [Proxy & Bridging](#7-proxy--bridging)
8. [Error Handling](#8-error-handling)
9. [Testing with Client](#9-testing-with-client)
10. [CLIO Integration Patterns](#10-clio-integration-patterns)

---

## 1. Client Basics

### Constructor

```python
from fastmcp import Client

client = Client(
    server_or_url: FastMCP | str | dict,       # Server, URL, path, or config
    auto_initialize: bool = True,               # Auto-initialize on connect
    timeout: float | None = None,               # Default timeout (seconds)
    log_handler: Callable | None = None,        # Log message callback
    progress_handler: Callable | None = None,   # Progress notification callback
    sampling_handler: Callable | None = None,   # LLM sampling callback
    elicitation_handler: Callable | None = None, # User input callback
    roots_handler: Callable | None = None,      # Filesystem roots callback (v2.0.0+)
    roots: list[str] | Callable | None = None,  # Static or dynamic roots (v2.0.0+)
    auth: BearerAuth | None = None,             # Authentication for HTTP transports
)
```

### Context Manager Pattern (Required)

```python
from fastmcp import Client

async with Client("http://localhost:8000/mcp") as client:
    tools = await client.list_tools()
    result = await client.call_tool("add", {"a": 1, "b": 2})
    print(result)
```

The context manager handles connection setup and teardown. Do not call methods outside the `async with` block.

### Client Properties

```python
async with Client(server) as client:
    # Connection status
    client.is_connected()  # bool

    # Server info (after initialization)
    client.initialize_result.serverInfo.name     # Server name
    client.initialize_result.instructions        # Server instructions
```

### Manual Initialization

```python
client = Client("server.py", auto_initialize=False)

async with client:
    print(client.is_connected())            # True
    print(client.initialize_result is None)  # True (not yet initialized)
    result = await client.initialize(timeout=10.0)
    # Now ready for operations
```

---

## 2. Transport System

### Auto-Detection

The Client automatically selects transport based on the input type:

| Input | Transport | Example |
|-------|-----------|---------|
| `FastMCP` instance | In-memory | `Client(my_server)` |
| HTTP/HTTPS URL | StreamableHttpTransport | `Client("http://localhost:8000/mcp")` |
| `.py` file path | StdioTransport (subprocess) | `Client("my_server.py")` |
| `dict` config | Multi-server | `Client({"mcpServers": {...}})` |
| Transport object | Uses directly | `Client(transport)` |

### In-Memory (Direct Server Instance)

No subprocess or network — fastest option, ideal for testing. Shares same memory space and environment variables:

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

### Explicit Transport Classes

For fine-grained control, use transport classes directly:

```python
from fastmcp.client.transports import (
    StdioTransport,
    StreamableHttpTransport,
    SSETransport,
    NpxStdioTransport,
    UvxStdioTransport,
)
```

**StdioTransport:**
```python
transport = StdioTransport(
    command="python",                       # Executable to launch
    args=["my_server.py", "--verbose"],     # Command-line arguments
    env={"API_KEY": "secret"},             # Env vars (isolated, NOT inherited from shell)
    cwd="/path/to/server",                 # Working directory
    keep_alive=True,                       # Maintain session across contexts (default: True)
)
client = Client(transport)
```

**Important:** STDIO servers do NOT inherit shell environment variables. Must pass explicitly via `env`.

**StreamableHttpTransport:**
```python
transport = StreamableHttpTransport(
    url="https://api.example.com/mcp",
    headers={"X-Custom-Header": "value"},
    auth="<your-token>",                   # Bearer token shorthand
)
client = Client(transport)
```

**SSETransport (legacy):**
```python
transport = SSETransport(
    url="https://api.example.com/sse",
    headers={"Authorization": "Bearer token"},
)
```

**Package-based transports:**
```python
# npm packages
transport = NpxStdioTransport(package="@modelcontextprotocol/server-github")

# Python packages via uvx
transport = UvxStdioTransport(
    tool_name="mcp-server-sqlite",
    tool_args=["--db", "data.db"],
)
```

---

## 3. Core Operations

### Tools

```python
async with Client(server) as client:
    # List all available tools
    tools = await client.list_tools()
    for tool in tools:
        print(f"{tool.name}: {tool.description}")
        print(f"  Schema: {tool.inputSchema}")

    # Call a tool
    result = await client.call_tool(
        name="add",                          # Tool name
        arguments={"a": 5, "b": 3},         # Parameters
        timeout=30.0,                        # Per-call timeout (optional)
        raise_on_error=True,                 # Raise ToolError on failure (default: True)
        meta={"trace_id": "abc123"},         # Metadata for observability (v2.13.1+)
    )

    # CallToolResult properties
    result.data                # Fully hydrated Python objects (v2.13.0+)
    result.content             # Standard MCP content blocks (TextContent, ImageContent, etc.)
    result.structured_content  # Raw JSON as server output schema
    result.is_error            # Boolean failure indicator

    # Raw protocol access (no deserialization)
    raw = await client.call_tool_mcp("add", {"a": 5, "b": 3})
```

### Resources

```python
async with Client(server) as client:
    resources = await client.list_resources()
    templates = await client.list_resource_templates()

    # Read a resource
    contents = await client.read_resource("file:///data/config.yaml")
    for item in contents:
        print(item.text or item.blob)
        print(item.mimeType)

    # Raw protocol access
    raw = await client.read_resource_mcp("file:///data/config.yaml")
```

### Prompts

```python
async with Client(server) as client:
    prompts = await client.list_prompts()

    prompt_result = await client.get_prompt("analyze_data", {"filepath": "/data/exp.h5"})
    for msg in prompt_result.messages:
        print(f"{msg.role}: {msg.content}")

    # Raw protocol access
    raw = await client.get_prompt_mcp("analyze_data", {"filepath": "/data/exp.h5"})
```

### Connectivity & Subscriptions

```python
async with Client(server) as client:
    await client.ping()  # Test connectivity

    # Resource subscriptions (v3.0.0+)
    async def on_update(uri: str):
        contents = await client.read_resource(uri)
        print(f"Resource updated: {uri}")

    await client.subscribe_resource("file:///data/config.yaml", on_update)
```

---

## 4. Client Handlers

Handlers enable the client to respond to server-initiated requests during tool execution.

### Log Handler

```python
from fastmcp.client.logging import LogMessage

async def log_handler(message: LogMessage):
    print(f"Server log: {message.data}")

client = Client("server.py", log_handler=log_handler)
```

### Progress Handler

```python
async def progress_handler(progress: float, total: float | None, message: str | None):
    if total:
        print(f"Progress: {progress / total * 100:.0f}%")
    else:
        print(f"Progress: {progress}")

client = Client("server.py", progress_handler=progress_handler)
```

### Sampling Handler

Allows the server to request LLM completions from the client:

```python
from fastmcp.client.sampling import SamplingMessage, SamplingParams

async def sampling_handler(
    messages: list[SamplingMessage],
    params: SamplingParams,
    context: RequestContext,
) -> str:
    response = await my_llm.complete(messages=messages)
    return response

client = Client("server.py", sampling_handler=sampling_handler)
```

**Built-in handlers:**
```python
from fastmcp.client.sampling import OpenAISamplingHandler     # v2.11.0+
from fastmcp.client.sampling import AnthropicSamplingHandler  # v2.14.1+

client = Client("server.py", sampling_handler=OpenAISamplingHandler(api_key="..."))
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
    user_input = input(f"{message}: ")

    # Direct return (implicit accept)
    if response_type:
        return response_type(value=user_input)

    # Explicit control
    return ElicitResult(action="accept", content=response_type(value=user_input))
    # ElicitResult(action="decline")  — user declined to provide input
    # ElicitResult(action="cancel")   — cancel entire operation

client = Client("server.py", elicitation_handler=elicitation_handler)
```

### Roots

Provide filesystem root boundaries to the server:

```python
from fastmcp.client.roots import RequestContext

# Static roots
client = Client("server.py", roots=["/data/experiments", "/data/configs"])

# Dynamic roots via callback
async def roots_callback(context: RequestContext) -> list[str]:
    print(f"Server requested roots (Request ID: {context.request_id})")
    return ["/data/experiments", "/data/configs"]

client = Client("server.py", roots=roots_callback)
```

---

## 5. Authentication

### BearerAuth

```python
from fastmcp.client.auth import BearerAuth

client = Client("https://api.example.com/mcp", auth=BearerAuth("your-token"))
```

### Custom Headers via Transport

```python
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="https://api.example.com/mcp",
    headers={
        "Authorization": "Bearer your-token",
        "X-API-Key": "key-value",
    },
)
client = Client(transport)
```

### Token Shorthand on Transport

```python
transport = StreamableHttpTransport(
    url="http://fastmcp.cloud/mcp",
    auth="<your-token>",  # Shorthand for Bearer token
)
```

---

## 6. Multi-Server Configuration

Connect to multiple MCP servers through a single client:

```python
config = {
    "mcpServers": {
        "data_tools": {
            "url": "http://localhost:8001/mcp",
            "transport": "streamable-http"
        },
        "io_tools": {
            "url": "http://localhost:8002/mcp",
            "transport": "http"
        },
        "local_tools": {
            "command": "python",
            "args": ["local_server.py"],
            "env": {"DEBUG": "true"}
        }
    }
}

async with Client(config) as client:
    tools = await client.list_tools()
    result = await client.call_tool("data_tools_search", {"query": "experiment"})
```

### Namespacing

| Component Type | Pattern |
|----------------|---------|
| Tools | `{server_name}_{tool_name}` |
| Prompts | `{server_name}_{prompt_name}` |
| Resources | `protocol://{server_name}/path` |

### Per-Server Tool Transformations

```python
config = {
    "mcpServers": {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http",
            "tools": {
                "weather_get_forecast": {
                    "name": "miami_weather",           # Rename tool
                    "description": "Get Miami weather", # Override description
                    "enabled": True,                    # Enable/disable
                    "tags": ["forecast"],               # Add tags
                    "arguments": {
                        "city": {
                            "default": "Miami",         # Set default value
                            "hide": True,               # Hide from LLM
                        }
                    }
                }
            },
            "include_tags": ["forecast"],   # Only include tools with these tags
            "exclude_tags": ["internal"],   # Exclude tools with these tags
        }
    }
}
```

---

## 7. Proxy & Bridging

### create_proxy()

Create a proxy server that forwards requests to a backend (v2.0.0+):

```python
from fastmcp.server import create_proxy

# From URL, file path, transport, config dict, or connected Client
proxy = create_proxy("http://backend:8000/mcp", name="MyProxy")
proxy.run()  # Defaults to stdio

# Transport bridging: stdio -> HTTP
proxy = create_proxy("backend_server.py", name="StdioToHTTP")
proxy.run(transport="http", host="0.0.0.0", port=8080)
```

### Session Isolation

```python
# Isolated (default, recommended) — each client gets own backend session
proxy = create_proxy("backend_server.py")

# Shared (single-threaded only) — all clients share one session
async with Client("backend_server.py") as connected:
    proxy = create_proxy(connected)
```

### ProxyClient (Low-Level)

```python
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient

# Disable specific MCP feature forwarding
backend = ProxyClient(
    "backend_server.py",
    sampling_handler=None,  # Disable LLM sampling forwarding
    log_handler=None,       # Disable log message forwarding
)

# Explicit session factory
proxy = FastMCPProxy(client_factory=lambda: ProxyClient("backend_server.py"))
```

### Mounting Proxies

```python
from fastmcp import FastMCP
from fastmcp.server import create_proxy

server = FastMCP("Combined")

@server.tool
def local_compute(data: str) -> str:
    """Local computation."""
    return process(data)

remote = create_proxy("http://remote:8000/mcp")
server.mount(remote, namespace="ext")  # namespace kwarg: v3.0.0+
```

### mount() vs import_server()

| | `mount()` | `import_server()` |
|---|-----------|-------------------|
| Type | Live dynamic linking | Static one-time copy |
| Remote changes | Reflected immediately | Not reflected |
| Latency | Delegation overhead | Same as local |
| Use case | Dynamic/remote servers | Performance-critical |

**Performance comparison:**

| Operation | Local | Proxied (HTTP) |
|-----------|-------|----------------|
| list_tools() | 1-2ms | 300-400ms |
| call_tool() | 1-2ms | 200-500ms |

---

## 8. Error Handling

### Tool Errors

```python
from fastmcp.exceptions import ToolError

# Exception mode (default: raise_on_error=True)
try:
    result = await client.call_tool("risky_tool", {"input": "data"})
except ToolError as e:
    print(f"Tool failed: {e}")

# Manual mode (raise_on_error=False)
result = await client.call_tool("risky_tool", {"input": "data"}, raise_on_error=False)
if result.is_error:
    print(f"Error: {result.content[0].text}")
else:
    print(f"Success: {result.data}")
```

**Server-side:** Use `ToolError` to send specific error messages that bypass `mask_error_details`:
```python
from fastmcp.exceptions import ToolError
raise ToolError(msg="File not found: experiment.h5")
```

### Connection Errors

```python
try:
    async with Client("http://unreachable:8000/mcp") as client:
        await client.ping()
except ConnectionError as e:
    print(f"Cannot connect: {e}")
except TimeoutError as e:
    print(f"Connection timed out: {e}")
```

### Timeout Configuration

```python
# Global timeout
client = Client("server.py", timeout=30.0)

async with client:
    # Per-call timeout
    result = await client.call_tool("slow_analysis", {"file": "big.h5"}, timeout=60.0)

    # Initialize timeout
    await client.initialize(timeout=10.0)
```

---

## 9. Testing with Client

### pytest Fixture Pattern

```python
import pytest
from fastmcp import FastMCP, Client
from fastmcp.client.transports import FastMCPTransport

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

# Reusable client fixture
@pytest.fixture
async def client(server):
    async with Client(server) as c:
        yield c
```

**pyproject.toml config:**
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### Basic Tests

```python
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

### Snapshot Testing

```python
from inline_snapshot import snapshot

@pytest.mark.anyio
async def test_tool_schema(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        assert tools == snapshot()  # Run with: pytest --inline-snapshot=fix,create
```

### Testing with Dependencies

```python
from fastmcp.server import Context

@pytest.fixture
def server_with_db():
    mcp = FastMCP("DBServer")
    mcp._test_db = {"users": [{"name": "Alice"}]}

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

---

## 10. CLIO Integration Patterns

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

tools = await load_mcp_tools("http://localhost:8000/mcp")
agent = dspy.ReAct("query -> answer", tools=tools)
```

### Health Check Pattern

```python
async def check_mcp_server(url: str) -> dict:
    try:
        async with Client(url, timeout=5.0) as client:
            await client.ping()
            tools = await client.list_tools()
            resources = await client.list_resources()
            return {
                "status": "healthy",
                "server": client.initialize_result.serverInfo.name,
                "tools": len(tools),
                "resources": len(resources),
            }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
```

### Gateway Client Pattern

```python
from fastmcp import FastMCP, Client

gateway = FastMCP("CLIO Gateway")

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

## Key Import Paths

```python
from fastmcp import Client, FastMCP
from fastmcp.client.transports import (
    StdioTransport, StreamableHttpTransport, SSETransport,
    FastMCPTransport, NpxStdioTransport, UvxStdioTransport,
)
from fastmcp.client.auth import BearerAuth
from fastmcp.client.sampling import (
    SamplingMessage, SamplingParams,
    OpenAISamplingHandler, AnthropicSamplingHandler,
)
from fastmcp.client.elicitation import ElicitResult, ElicitRequestParams
from fastmcp.client.logging import LogMessage
from fastmcp.client.roots import RequestContext
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient
from fastmcp.exceptions import ToolError
```

---

## Version Timeline

| Feature | Version |
|---------|---------|
| Client class, create_proxy(), Roots | 2.0.0+ |
| Mounting | 2.2.0+ |
| Multi-server config | 2.4.0+ |
| Argument serialization | 2.9.0+ |
| Session isolation improvements | 2.10.3+ |
| Mirrored components | 2.10.5+ |
| OpenAI sampling handler | 2.11.0+ |
| Structured output (.data) | 2.13.0+ |
| Meta parameter on call_tool | 2.13.1+ |
| Anthropic sampling handler | 2.14.1+ |
| Namespace keyword on mount | 3.0.0+ |
| Resource versioning & subscriptions | 3.0.0+ |

---

## Quick Reference

| Operation | Code |
|-----------|------|
| Connect | `async with Client(server) as client:` |
| List tools | `await client.list_tools()` |
| Call tool | `await client.call_tool("name", {"arg": "val"})` |
| Call tool (no raise) | `await client.call_tool("name", args, raise_on_error=False)` |
| Read resource | `await client.read_resource("uri://path")` |
| Get prompt | `await client.get_prompt("name", {"arg": "val"})` |
| Health check | `await client.ping()` |
| Subscribe | `await client.subscribe_resource(uri, callback)` |
| Raw protocol | `await client.call_tool_mcp("name", args)` |

**Transport Selection:**
- Testing → `Client(server_instance)` (in-memory, shared memory)
- Local dev → `Client("server.py")` (stdio subprocess, isolated env)
- Production → `Client("http://host:port/mcp")` (HTTP)
- Multi-server → `Client({"mcpServers": {...}})` (config)
- Auth → `Client(url, auth=BearerAuth("token"))` or transport headers

**See also:**
- [00_FASTMCP_API_REFERENCE.md](00_FASTMCP_API_REFERENCE.md) — Full API reference
- [03_COMPOSITION.md](03_COMPOSITION.md) — Server composition and mounting
- [06_TESTING.md](06_TESTING.md) — Testing patterns in depth
- [07_CLIO_PATTERNS.md](07_CLIO_PATTERNS.md) — CLIO-specific integration
