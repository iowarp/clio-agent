# FastMCP API Reference
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Complete API reference for FastMCP, the Python implementation of the Model Context Protocol (MCP). FastMCP enables you to build MCP servers that expose tools, resources, and prompts to LLM clients.

---

## Core Classes

### FastMCP

Main server class for creating MCP servers.

```python
from fastmcp import FastMCP

mcp = FastMCP(
    name: str,                              # Server name (required)
    mask_error_details: bool = False,       # Hide error details from clients
    strict_input_validation: bool = False,  # Strict vs flexible type coercion
    on_duplicate_tools: str = "warn",       # "warn", "error", "replace", "ignore"
    on_duplicate_resources: str = "warn",
    on_duplicate_prompts: str = "warn",
    lifespan: LifespanFunction | None = None  # v3.0.0+
)
```

**Methods**:
- `run(transport="stdio", host="127.0.0.1", port=8000)` - Run server (blocks)
- `run_async(...)` - Async version for embedding in event loops
- `http_app()` - Return ASGI application for deployment
- `mount(server, namespace=None)` - Mount another server (v3.0.0+)
- `add_transform(transform)` - Add transform to pipeline
- `enable(keys=None, tags=None, only=False)` - Control visibility (v3.0.0+)
- `disable(keys=None, tags=None)` - Hide components (v3.0.0+)

### Client

Client for connecting to MCP servers.

```python
from fastmcp import Client

# Auto-detect transport
client = Client(
    server_or_url: FastMCP | str | dict,  # Server instance, URL, path, or config
    auto_initialize: bool = True,          # Auto-initialize on connect
    timeout: float | None = None,          # Default timeout for operations
    log_handler: Callable | None = None,   # Logging callback
    progress_handler: Callable | None = None,
    sampling_handler: Callable | None = None,
    elicitation_handler: Callable | None = None,
    roots_handler: Callable | None = None
)

# Context manager pattern (required)
async with client:
    result = await client.call_tool("tool_name", {"arg": "value"})
```

**Transport Auto-Detection**:
- `FastMCP` instance → in-memory (no subprocess/network)
- `"http://..."` or `"https://..."` → HTTP transport
- `"/path/to/server.py"` or `"server.py"` → STDIO subprocess
- `{"mcpServers": {...}}` → Multi-server config

**Methods**:
- `ping()` → void - Test connectivity
- `list_tools()` → list[Tool] - List available tools
- `list_resources()` → list[Resource] - List resources
- `list_resource_templates()` → list[ResourceTemplate] - List templates
- `list_prompts()` → list[Prompt] - List prompts
- `call_tool(name, arguments)` → CallToolResult - Execute tool
- `read_resource(uri)` → list[ResourceContents] - Read resource
- `get_prompt(name, arguments)` → GetPromptResult - Get prompt messages
- `subscribe_resource(uri, callback)` → void - Subscribe to updates (v3.0.0+)

---

## Decorators

### @mcp.tool

Expose Python functions as MCP tools.

```python
@mcp.tool(
    name: str | None = None,              # Override function name
    description: str | None = None,        # Override docstring
    tags: set[str] | None = None,         # Categorization tags
    meta: dict[str, Any] | None = None,   # Custom metadata
    icons: list[Icon] | None = None,      # Visual icons (v2.13.0+)
    annotations: dict | None = None,       # MCP behavior hints
    timeout: float | None = None,          # Execution timeout (v3.0.0+)
    version: str | int | None = None,     # Version identifier (v3.0.0+)
    output_schema: dict | None = None     # Custom result schema
)
def tool_function(
    arg1: int,
    arg2: str = "default",
    ctx: Context = CurrentContext()  # Optional context injection
) -> ReturnType:
    """Tool description from docstring."""
    return result
```

**Type Annotations Supported**:
- Scalar: `int`, `float`, `str`, `bool`, `bytes`
- Temporal: `datetime`, `date`, `timedelta`
- Collections: `list[T]`, `dict[K, V]`, `set[T]`
- Constrained: `Literal["A", "B"]`, Enum
- Advanced: `Path`, `UUID`, Pydantic models, dataclasses
- Optional: `T | None`, `Union[T, U]`

**Return Types**:
- `str` → TextContent
- `bytes` → BlobResourceContents (base64)
- `Image` → ImageContent
- `Audio` → AudioContent
- `File` → EmbeddedResource
- `dict`/Pydantic/dataclass → Structured output
- `ToolResult` → Full control over result structure

### @mcp.resource

Expose static or parameterized data as resources.

```python
@mcp.resource(
    uri: str,                             # Required: resource URI (may have {params})
    name: str | None = None,
    description: str | None = None,
    mime_type: str | None = None,         # Content type
    tags: set[str] | None = None,
    icons: list[Icon] | None = None,
    annotations: dict | None = None,
    meta: dict[str, Any] | None = None,
    version: str | int | None = None
)
def resource_function(
    param1: str,  # From URI template {param1}
    query_param: int = 10,  # From query string {?query_param}
    ctx: Context = CurrentContext()
) -> str | bytes | ResourceResult:
    """Resource description."""
    return content
```

**URI Templates**:
- Static: `"resource://config"` - no parameters
- Parameters: `"weather://{city}/current"` - single param
- Multiple: `"repos://{owner}/{repo}/info"` - multiple params
- Wildcards: `"path://{filepath*}"` - greedy capture (v2.2.4+)
- Query params: `"data://{id}{?format,limit}"` - optional query (v2.13.0+)

**Return Types**:
- `str` → TextResourceContents (mime_type defaults to "text/plain")
- `bytes` → BlobResourceContents (base64 encoded)
- `ResourceResult` → Full control (v3.0.0+)

### @mcp.prompt

Define reusable prompt templates.

```python
@mcp.prompt(
    name: str | None = None,
    title: str | None = None,             # Human-readable name
    description: str | None = None,
    tags: set[str] | None = None,
    meta: dict[str, Any] | None = None,
    icons: list[Icon] | None = None,
    version: str | int | None = None
)
def prompt_function(
    arg1: str,
    arg2: list[int] | None = None,  # Complex types supported (v2.9.0+)
    ctx: Context = CurrentContext()
) -> str | list[Message] | PromptResult:
    """Prompt description."""
    return messages
```

**Return Types**:
- `str` → Single user message
- `list[Message | str]` → Multi-turn conversation
- `PromptResult` → Full control with metadata (v3.0.0+)

---

## Context & Dependencies

### Context Object

Request-scoped context providing logging, progress tracking, and client capabilities.

```python
from fastmcp import Context
from fastmcp.dependencies import CurrentContext

@mcp.tool
async def process(data: str, ctx: Context = CurrentContext()) -> dict:
    # Logging
    await ctx.debug("Debug message")
    await ctx.info(f"Processing {len(data)} bytes")
    await ctx.warning("Deprecated parameter used")
    await ctx.error("Operation failed")

    # Progress reporting
    await ctx.report_progress(progress=50, total=100)

    # Resource access
    resources = await ctx.list_resources()
    content = await ctx.read_resource("resource://config")

    # Prompt access (v2.13.0+)
    prompts = await ctx.list_prompts()
    result = await ctx.get_prompt("analyze", {"data": data})

    # LLM sampling (v2.0.0+)
    summary = await ctx.sample("Summarize: " + data, temperature=0.7)

    # Client elicitation (v2.10.0+)
    result = await ctx.elicit("Enter name:", response_type=str)
    if result.action == "accept":
        name = result.data

    # Session state (v3.0.0+)
    count = await ctx.get_state("counter") or 0
    await ctx.set_state("counter", count + 1)
    await ctx.delete_state("counter")

    # Metadata
    request_id = ctx.request_id
    client_id = ctx.client_id
    session_id = ctx.session_id
    transport = ctx.transport  # "stdio", "sse", "streamable-http", or None

    # Lifespan context access
    config = ctx.lifespan_context.get("config", {})

    return {"status": "complete"}
```

**Context Access Methods**:
1. `CurrentContext()` - Preferred dependency injection (v2.14+)
2. Type hint `ctx: Context` - Legacy direct injection
3. `get_context()` - Function-level accessor (v2.2.11+)

### Depends()

Dependency injection for custom dependencies.

```python
from fastmcp.dependencies import Depends
from contextlib import asynccontextmanager

# Simple dependency
def get_config() -> dict:
    return {"api_url": "https://api.example.com", "timeout": 30}

# Async dependency
async def get_db_connection():
    return await connect_to_database()

# Async context manager (resource cleanup)
@asynccontextmanager
async def get_database():
    db = await connect_to_database()
    try:
        yield db
    finally:
        await db.close()

@mcp.tool
async def fetch_data(
    query: str,
    config: dict = Depends(get_config),        # Injected, hidden from LLM
    db = Depends(get_database)                 # Context manager handled
) -> str:
    result = await db.execute(query)
    return f"Fetched from {config['api_url']}: {result}"
```

**Features**:
- Parameters with `Depends()` excluded from MCP schema (hidden from LLM)
- Dependencies cached per-request (shared across multiple parameters)
- Supports sync functions, async functions, async context managers
- Nested dependencies with automatic ordering

### Built-in Dependencies

```python
from fastmcp.dependencies import (
    CurrentContext,      # MCP context for logging/progress
    CurrentFastMCP,      # Server instance access (v2.14+)
    CurrentRequest,      # Starlette HTTP request (v2.2.11+, HTTP transport only)
    CurrentHeaders,      # HTTP headers with fallback (v2.2.11+)
    CurrentAccessToken,  # OAuth token data (v2.11.0+)
    CurrentDocket,       # Task scheduling (v2.3.0+)
    CurrentWorker,       # Worker metadata (v2.3.0+)
    Progress,            # Task progress tracking (v2.3.0+)
)

@mcp.tool
async def advanced_tool(
    data: str,
    ctx: Context = CurrentContext(),
    server: FastMCP = CurrentFastMCP(),
    request: Request = CurrentRequest(),
    headers: dict = CurrentHeaders()
) -> dict:
    return {"processed": True}
```

---

## Composition

### mount()

Mount another MCP server into the current server (live binding).

```python
from fastmcp import FastMCP
from fastmcp.server import create_proxy

main = FastMCP("MainServer")
api_server = FastMCP("APIServer")

# Mount local server with namespace
main.mount(api_server, namespace="api")
# Tools: api_add, api_multiply
# Resources: resource://api/data

# Mount without namespace
main.mount(api_server)
# Tools: add, multiply (no prefix)

# Mount remote server via proxy
main.mount(
    create_proxy("http://example.com/mcp"),
    namespace="remote"
)

# Mount from config
github_config = {
    "mcpServers": {
        "default": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"]
        }
    }
}
main.mount(create_proxy(github_config), namespace="github")
```

**Characteristics**:
- Live binding: changes in mounted server reflected immediately
- All lifespans executed
- Most recently mounted server wins for name conflicts (v3.0.0+)
- Parent tag filters apply recursively

### import_server()

Import another server's components (static copy).

```python
main = FastMCP("MainServer")
static_server = FastMCP("StaticServer")

# Components copied once at import time
await main.import_server(static_server, namespace="static")
```

**Characteristics**:
- One-time copy at import
- No live updates
- Faster for large hierarchies

### create_proxy()

Create a proxy server that forwards to a backend MCP server.

```python
from fastmcp.server import create_proxy

# From URL
proxy = create_proxy("http://example.com/mcp", name="MyProxy")

# From local script
proxy = create_proxy("./backend_server.py", name="LocalProxy")

# From config
config = {
    "mcpServers": {
        "default": {"url": "https://api.example.com/mcp"}
    }
}
proxy = create_proxy(config, name="ConfigProxy")

# From transport
from fastmcp.client.transports import NpxStdioTransport
proxy = create_proxy(
    NpxStdioTransport(package="@modelcontextprotocol/server-github"),
    name="GitHubProxy"
)

if __name__ == "__main__":
    proxy.run()  # Transport bridging: HTTP backend → stdio frontend
```

---

## Transforms

Transforms modify component behavior in a pipeline: `Provider → [Transform A] → [Transform B] → Client`

### Built-in Transforms

#### Namespace

Add prefixes to prevent naming conflicts.

```python
from fastmcp.server.transforms import Namespace

main.add_transform(Namespace("v1"))
# Tools: v1_add, v1_multiply
# Resources: resource://v1/data
```

#### ToolTransform

Rename tools, modify descriptions, reshape arguments.

```python
from fastmcp.server.transforms import ToolTransform, ToolTransformConfig

main.add_transform(ToolTransform({
    "verbose_tool_name": ToolTransformConfig(
        name="short",
        description="Simplified description",
        # argument_mappings for param renaming
    )
}))
```

#### Enabled (Visibility)

Control component visibility at runtime (renamed to Visibility in v3.0.0b1).

```python
# Disable specific components
main.disable(keys={"tool:admin_action"})
main.disable(tags={"admin", "internal"})

# Enable only specific tags
main.enable(tags={"public", "read-only"}, only=True)
```

### Custom Transform

```python
from fastmcp.server.transforms import Transform, GetToolNext
from fastmcp.tools.tool import Tool
from collections.abc import Sequence

class TagFilter(Transform):
    """Filter tools to only those with specific tags."""

    def __init__(self, required_tags: set[str]):
        self.required_tags = required_tags

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t for t in tools if t.tags & self.required_tags]

    async def get_tool(self, name: str, call_next: GetToolNext) -> Tool | None:
        tool = await call_next(name)
        if tool and tool.tags & self.required_tags:
            return tool
        return None

main.add_transform(TagFilter({"public", "safe"}))
```

---

## Transports

### STDIO (Default)

Standard input/output for local development and CLI tools.

```python
mcp.run()  # or mcp.run(transport="stdio")
```

- Client spawns subprocess per session
- Server doesn't persist between sessions
- Ideal for: Claude Desktop, local dev, CLI tools

### HTTP (Streamable)

Production-ready HTTP transport with bidirectional communication.

```python
mcp.run(transport="http", host="127.0.0.1", port=8000)
# Server accessible at http://localhost:8000/mcp
```

- Multiple concurrent clients
- Persistent server
- Custom routes supported

### SSE (Legacy)

Server-Sent Events transport (deprecated, use HTTP instead).

```python
mcp.run(transport="sse", host="127.0.0.1", port=8000)
```

---

## Testing

### In-Memory Client Pattern

```python
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from my_project.main import mcp

@pytest.fixture
async def mcp_client():
    async with Client(transport=mcp) as client:
        yield client

async def test_tool(mcp_client: Client[FastMCPTransport]):
    result = await mcp_client.call_tool("add", {"a": 2, "b": 3})
    assert result.data == 5
```

**Setup**:
```bash
pip install pytest-asyncio
```

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

---

## Quick-Start Example

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import CurrentContext, Depends
from typing import Annotated
from pydantic import Field

mcp = FastMCP("CalculatorServer")

# Tool with type hints
@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

# Tool with context
@mcp.tool
async def multiply(
    a: Annotated[int, Field(ge=0, description="First number")],
    b: Annotated[int, Field(ge=0, description="Second number")],
    ctx: Context = CurrentContext()
) -> int:
    """Multiply two non-negative integers."""
    await ctx.info(f"Multiplying {a} * {b}")
    return a * b

# Tool with dependency injection
def get_api_key() -> str:
    return "secret-key-from-env"

@mcp.tool
async def call_api(
    query: str,
    api_key: str = Depends(get_api_key)  # Hidden from LLM
) -> dict:
    """Call external API with authenticated key."""
    return {"query": query, "authenticated": True}

# Resource
@mcp.resource("config://settings")
def get_config() -> str:
    """Server configuration."""
    return '{"version": "1.0", "mode": "production"}'

# Parameterized resource
@mcp.resource("user://{user_id}/profile")
def get_user_profile(user_id: str) -> dict:
    """Get user profile by ID."""
    return {"user_id": user_id, "name": "John Doe"}

# Prompt
@mcp.prompt
def code_review(language: str, code: str) -> str:
    """Generate code review request."""
    return f"Please review this {language} code:\n\n```{language}\n{code}\n```"

if __name__ == "__main__":
    mcp.run()  # STDIO by default
    # mcp.run(transport="http", port=8000)  # HTTP server
```

**Client Usage**:
```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("./calculator_server.py") as client:
        # Call tool
        result = await client.call_tool("add", {"a": 5, "b": 3})
        print(result.data)  # 8

        # Read resource
        config = await client.read_resource("config://settings")
        print(config[0].text)

        # Get prompt
        messages = await client.get_prompt("code_review", {
            "language": "python",
            "code": "def add(a, b): return a + b"
        })
        print(messages.messages)

asyncio.run(main())
```

---

## Version Notes

This reference covers FastMCP 2.x with v3.0.0 beta features. Key version differences:

- **v2.14.0+**: `CurrentContext()`, `CurrentFastMCP()`, dependency injection
- **v2.13.0+**: Query parameters in resources, strict validation mode, icons
- **v2.11.0+**: Meta field, OAuth access tokens
- **v2.10.0+**: Structured output, client elicitation
- **v2.9.0+**: Complex prompt arguments (lists, dicts)
- **v3.0.0+**: Lifespan decorator, timeouts, ResourceResult/PromptResult, session state, visibility API changes

For complete changelog: https://gofastmcp.com

---

**FastMCP** is the fastest way to build MCP servers in Python. For more examples and patterns, see the companion guides:
- `01_SERVER_CREATION.md` - Detailed server creation guide
- `02_CLIENT_USAGE.md` - Client integration patterns
