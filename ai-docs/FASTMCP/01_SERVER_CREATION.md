# FastMCP Server Creation
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Comprehensive guide to creating FastMCP servers that expose tools, resources, and prompts to LLM clients.

---

## Server Initialization

### Basic Server

```python
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

if __name__ == "__main__":
    mcp.run()  # STDIO transport by default
```

### Configuration Options

```python
mcp = FastMCP(
    name="AdvancedServer",

    # Error handling
    mask_error_details=True,              # Hide internal errors from clients

    # Validation
    strict_input_validation=True,         # Reject type mismatches (default: coerce)

    # Duplicate handling
    on_duplicate_tools="error",           # "warn", "error", "replace", "ignore"
    on_duplicate_resources="replace",
    on_duplicate_prompts="ignore",

    # Lifespan (v3.0.0+)
    lifespan=app_lifespan                 # Initialization/cleanup function
)
```

---

## Tools (@mcp.tool)

Tools are Python functions exposed to LLMs for execution.

### Basic Tool

```python
@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers together."""
    return a + b
```

**Key Requirements**:
- Type hints on all parameters (required)
- Docstring for description (shown to LLM)
- Return type hint (optional but recommended)

### Type Annotations

FastMCP supports rich type annotations for automatic schema generation:

```python
from typing import Annotated, Literal
from datetime import datetime, date
from pathlib import Path
from uuid import UUID
from enum import Enum
from pydantic import BaseModel, Field

class Priority(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class Task(BaseModel):
    title: str
    priority: Priority
    due_date: date | None = None

@mcp.tool
def create_task(
    # Scalar types
    title: str,
    count: int,
    weight: float,
    active: bool,

    # Collections
    tags: list[str],
    metadata: dict[str, int],
    unique_ids: set[str],

    # Temporal
    created_at: datetime,
    due_date: date,

    # Constrained
    status: Literal["pending", "active", "done"],
    priority: Priority,

    # Advanced
    file_path: Path,
    task_id: UUID,
    task_data: Task,  # Pydantic model

    # Optional
    description: str | None = None,
    assigned_to: str = "unassigned"
) -> dict:
    """Create a new task with rich type validation."""
    return {
        "task_id": str(task_id),
        "title": title,
        "priority": priority.value
    }
```

**Unsupported**: Functions with `*args` or `**kwargs` (schema cannot be generated).

### Parameter Metadata with Annotated

Use `Annotated` with Pydantic `Field` for rich constraints and descriptions:

```python
from typing import Annotated
from pydantic import Field

@mcp.tool
def process_image(
    image_url: Annotated[str, "URL of the image to process"],
    width: Annotated[int, Field(ge=1, le=2000, description="Width in pixels")] = 800,
    height: Annotated[int, Field(ge=1, le=2000, description="Height in pixels")] = 600,
    quality: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8,
    format: Annotated[Literal["png", "jpg", "webp"], Field(description="Output format")] = "png"
) -> dict:
    """Process image with size and quality constraints."""
    return {
        "url": image_url,
        "dimensions": f"{width}x{height}",
        "quality": quality,
        "format": format
    }
```

### Async and Sync Functions

Both `async def` and `def` work. Sync functions run in thread pools automatically.

```python
@mcp.tool
async def fetch_data(url: str) -> dict:
    """Async tool using httpx or aiohttp."""
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()

@mcp.tool
def blocking_computation(n: int) -> int:
    """Sync tool for CPU-bound work."""
    import time
    time.sleep(1)  # Runs in thread pool, doesn't block event loop
    return n * 2
```

### Return Types

FastMCP automatically converts return types to MCP content blocks:

```python
from fastmcp.utilities.types import Image, Audio, File
from fastmcp.tools.tool import ToolResult

# Text content
@mcp.tool
def get_summary() -> str:
    """Returns TextContent."""
    return "This is a summary."

# Binary content
@mcp.tool
def get_file_bytes() -> bytes:
    """Returns BlobResourceContents (base64 encoded)."""
    return b"binary data"

# Image content
@mcp.tool
def generate_chart() -> Image:
    """Returns ImageContent."""
    return Image(path="chart.png")  # or Image(data=bytes, mime_type="image/png")

# Audio content
@mcp.tool
def generate_audio() -> Audio:
    """Returns AudioContent."""
    return Audio(path="speech.mp3")

# File/resource embedding
@mcp.tool
def get_document() -> File:
    """Returns EmbeddedResource."""
    return File(path="document.pdf")

# Structured output (auto-generated structuredContent)
@mcp.tool
def get_data() -> dict:
    """Returns structured output."""
    return {"status": "success", "count": 42, "items": [1, 2, 3]}

# Pydantic model (also structured)
from pydantic import BaseModel

class Report(BaseModel):
    title: str
    data: list[float]

@mcp.tool
def generate_report() -> Report:
    """Returns structured output from Pydantic model."""
    return Report(title="Q4 Results", data=[1.5, 2.3, 3.1])

# Full control with ToolResult
@mcp.tool
def advanced_tool() -> ToolResult:
    """Full control over result structure."""
    return ToolResult(
        content="Human-readable summary",
        structured_content={"detailed": "data", "metrics": [1, 2, 3]},
        meta={"execution_time_ms": 145, "cache_hit": True}
    )
```

### Decorator Options

```python
@mcp.tool(
    name="custom_tool_name",              # Override function name
    description="Custom description",      # Override docstring
    tags={"math", "basic"},               # Categorization for filtering
    meta={"version": "2.1", "author": "team"},  # Custom metadata (v2.11.0+)
    timeout=30.0,                         # Execution timeout in seconds (v3.0.0+)
    version="2.1",                        # Version identifier (v3.0.0+)
    icons=[Icon(...)],                    # Visual icons (v2.13.0+)
    annotations={                         # MCP behavior hints
        "title": "Friendly Tool Name",
        "readOnlyHint": True,             # Doesn't modify state
        "destructiveHint": False,         # Not destructive
        "idempotentHint": True,           # Same result for same input
        "openWorldHint": False,           # Closed set of parameters
    }
)
def my_tool(x: int) -> int:
    """Tool with all decorator options."""
    return x * 2
```

### Error Handling

```python
from fastmcp.exceptions import ToolError

@mcp.tool
def divide(a: float, b: float) -> float:
    """Divide two numbers with error handling."""
    if b == 0:
        raise ToolError("Cannot divide by zero")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise ToolError("Both arguments must be numbers")
    return a / b

# Server-level error masking
mcp = FastMCP(name="Server", mask_error_details=True)
# With masking: clients see "Tool execution failed" instead of internal details
```

### Timeouts (v3.0.0+)

```python
@mcp.tool(timeout=30.0)
async def fetch_external_data(url: str) -> dict:
    """Tool with 30-second timeout."""
    # If execution exceeds 30 seconds, raises TimeoutError
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=25.0)
        return response.json()
```

---

## Context Injection

The `Context` object provides access to logging, progress reporting, resource access, and client capabilities.

### Three Ways to Access Context

```python
from fastmcp import Context
from fastmcp.dependencies import CurrentContext
from fastmcp.server.dependencies import get_context

# Method 1: CurrentContext() - Preferred (v2.14+)
@mcp.tool
async def method1(data: str, ctx: Context = CurrentContext()) -> str:
    await ctx.info("Using CurrentContext()")
    return "done"

# Method 2: Type hint (Legacy)
@mcp.tool
async def method2(data: str, ctx: Context) -> str:
    await ctx.info("Using type hint")
    return "done"

# Method 3: get_context() - Function-level accessor (v2.2.11+)
@mcp.tool
async def method3(data: str) -> str:
    ctx = get_context()
    await ctx.info("Using get_context()")
    return "done"
```

### Context Capabilities

```python
@mcp.tool
async def comprehensive_tool(
    data: str,
    ctx: Context = CurrentContext()
) -> dict:
    # Logging (sent to client)
    await ctx.debug("Detailed debug information")
    await ctx.info(f"Processing {len(data)} bytes")
    await ctx.warning("Deprecated parameter format detected")
    await ctx.error("Non-fatal error occurred")

    # Progress reporting
    await ctx.report_progress(progress=0, total=100, description="Starting")
    # ... do work ...
    await ctx.report_progress(progress=50, total=100, description="Halfway")
    # ... do more work ...
    await ctx.report_progress(progress=100, total=100, description="Complete")

    # Resource access
    resources = await ctx.list_resources()
    content_list = await ctx.read_resource("resource://config")
    config_text = content_list[0].text if content_list else "{}"

    # Prompt access (v2.13.0+)
    prompts = await ctx.list_prompts()
    prompt_result = await ctx.get_prompt("analyze_data", {"dataset": "users"})

    # LLM sampling (v2.0.0+) - Ask client's LLM for help
    summary = await ctx.sample(
        f"Summarize this in 10 words: {data[:200]}",
        temperature=0.7,
        max_tokens=50
    )

    # Client elicitation (v2.10.0+) - Ask user for input
    result = await ctx.elicit("Enter output filename:", response_type=str)
    if result.action == "accept":
        filename = result.data
    else:
        filename = "default.txt"

    # Session state (v3.0.0+)
    counter = await ctx.get_state("invocation_count") or 0
    await ctx.set_state("invocation_count", counter + 1)

    # Request metadata
    request_id = ctx.request_id      # Unique request ID
    client_id = ctx.client_id        # Client identifier (may be None)
    session_id = ctx.session_id      # Session ID
    transport = ctx.transport        # "stdio", "sse", "streamable-http", or None

    # Lifespan context (v3.0.0+)
    config = ctx.lifespan_context.get("config", {})

    return {
        "processed": len(data),
        "summary": summary.text,
        "filename": filename,
        "invocations": counter + 1
    }
```

---

## Dependency Injection with Depends()

Hide parameters from the LLM schema and inject values at runtime.

### Basic Dependencies

```python
from fastmcp.dependencies import Depends

def get_api_key() -> str:
    """Load API key from environment."""
    import os
    return os.environ.get("API_KEY", "default-key")

def get_config() -> dict:
    """Load application config."""
    return {
        "api_url": "https://api.example.com",
        "timeout": 30,
        "retries": 3
    }

@mcp.tool
async def call_external_api(
    query: str,                          # Visible to LLM
    api_key: str = Depends(get_api_key), # Hidden from LLM
    config: dict = Depends(get_config)   # Hidden from LLM
) -> dict:
    """Call external API with authenticated credentials."""
    # api_key and config automatically injected
    return {
        "query": query,
        "api_url": config["api_url"],
        "authenticated": True
    }
```

**LLM sees**:
```json
{
  "name": "call_external_api",
  "description": "Call external API with authenticated credentials.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"}
    },
    "required": ["query"]
  }
}
```

### Async Dependencies

```python
async def get_database_connection():
    """Async dependency."""
    import asyncpg
    return await asyncpg.connect(
        host="localhost",
        database="mydb",
        user="user",
        password="password"
    )

@mcp.tool
async def query_database(
    sql: str,
    db = Depends(get_database_connection)
) -> list[dict]:
    """Execute SQL query."""
    rows = await db.fetch(sql)
    return [dict(row) for row in rows]
```

### Async Context Manager Dependencies

Use async context managers for automatic resource cleanup:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def get_managed_database():
    """Dependency with automatic cleanup."""
    import asyncpg
    db = await asyncpg.connect(
        host="localhost",
        database="mydb",
        user="user",
        password="password"
    )
    try:
        yield db
    finally:
        await db.close()  # Guaranteed cleanup

@mcp.tool
async def safe_query(
    sql: str,
    db = Depends(get_managed_database)  # Auto-closed after tool execution
) -> list[dict]:
    """Execute SQL query with automatic connection cleanup."""
    rows = await db.fetch(sql)
    return [dict(row) for row in rows]
```

### Nested Dependencies

Dependencies can depend on other dependencies:

```python
def get_db_url() -> str:
    import os
    return os.environ.get("DATABASE_URL", "postgresql://localhost/db")

async def get_db_pool(db_url: str = Depends(get_db_url)):
    """Nested dependency."""
    import asyncpg
    return await asyncpg.create_pool(db_url)

@mcp.tool
async def execute_query(
    query: str,
    pool = Depends(get_db_pool)  # Automatically resolves get_db_url first
) -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(query)
```

### Built-in Dependencies

```python
from fastmcp import Context, FastMCP
from fastmcp.dependencies import (
    CurrentContext,      # MCP context
    CurrentFastMCP,      # Server instance (v2.14+)
    CurrentRequest,      # HTTP request (v2.2.11+, HTTP transport only)
    CurrentHeaders,      # HTTP headers (v2.2.11+)
    CurrentAccessToken,  # OAuth token (v2.11.0+)
)

@mcp.tool
async def advanced_tool(
    data: str,
    ctx: Context = CurrentContext(),
    server: FastMCP = CurrentFastMCP(),
    request = CurrentRequest(),  # None if not HTTP transport
    headers: dict = CurrentHeaders()  # Empty dict if not HTTP
) -> dict:
    """Tool with multiple built-in dependencies."""
    server_name = server.name
    user_agent = headers.get("User-Agent", "unknown")

    return {
        "server": server_name,
        "user_agent": user_agent,
        "data_length": len(data)
    }
```

### Dependency Caching

Dependencies are cached per-request. Multiple parameters using the same dependency share the same instance:

```python
def expensive_dependency() -> dict:
    """Called once per request, cached."""
    import time
    time.sleep(1)  # Expensive operation
    return {"data": "value"}

@mcp.tool
def tool_a(dep: dict = Depends(expensive_dependency)) -> str:
    return dep["data"]

@mcp.tool
def tool_b(dep: dict = Depends(expensive_dependency)) -> str:
    return dep["data"]  # Same instance as tool_a if called in same request
```

---

## Resources (@mcp.resource)

Resources provide static or dynamic data accessible via URIs.

### Static Resources

```python
@mcp.resource("resource://greeting")
def get_greeting() -> str:
    """Simple greeting message."""
    return "Hello from FastMCP!"

@mcp.resource("config://settings")
def get_config() -> str:
    """Application configuration."""
    import json
    return json.dumps({
        "version": "1.0",
        "mode": "production",
        "features": ["auth", "logging"]
    })
```

### Parameterized Resources (Templates)

```python
# Single parameter
@mcp.resource("weather://{city}/current")
def get_weather(city: str) -> str:
    """Current weather for a city."""
    import json
    return json.dumps({
        "city": city.capitalize(),
        "temperature": 22,
        "conditions": "sunny"
    })

# Multiple parameters
@mcp.resource("repos://{owner}/{repo}/info")
def get_repo_info(owner: str, repo: str) -> str:
    """GitHub repository information."""
    import json
    return json.dumps({
        "owner": owner,
        "repo": repo,
        "stars": 1234
    })

# Wildcard parameter (v2.2.4+) - greedy match
@mcp.resource("path://{filepath*}")
def get_file_content(filepath: str) -> str:
    """Read file content from arbitrary path."""
    # filepath can contain slashes: path://docs/api/reference.md
    with open(filepath, 'r') as f:
        return f.read()

# Query parameters (v2.13.0+)
@mcp.resource("data://{id}{?format,limit}")
def get_data(id: str, format: str = "json", limit: int = 10) -> str:
    """Get data with optional format and limit."""
    # Called as: data://123?format=xml&limit=20
    if format == "xml":
        return f"<data id='{id}' limit='{limit}' />"
    import json
    return json.dumps({"id": id, "limit": limit})
```

### Return Types

```python
from fastmcp.resources import ResourceResult, ResourceContent

# String → TextResourceContents
@mcp.resource("resource://text")
def get_text() -> str:
    return "Plain text content"

# Bytes → BlobResourceContents (base64)
@mcp.resource("resource://binary")
def get_binary() -> bytes:
    return b"binary data"

# ResourceResult for full control (v3.0.0+)
@mcp.resource("resource://advanced")
def get_advanced() -> ResourceResult:
    return ResourceResult(
        contents=[
            ResourceContent(
                content='{"users": [{"id": 1}]}',
                mime_type="application/json"
            ),
            ResourceContent(
                content="# Users\n\nTotal: 1 user",
                mime_type="text/markdown"
            )
        ],
        meta={"total_users": 1, "cache_ttl": 300}
    )
```

### Resource Classes (Direct Registration)

For file-based or HTTP resources, use pre-built classes:

```python
from fastmcp.resources import (
    FileResource,
    TextResource,
    BinaryResource,
    DirectoryResource,
    HttpResource  # requires httpx
)
from pathlib import Path

# File resource
readme = FileResource(
    uri=f"file://{Path('README.md').absolute().as_posix()}",
    path=Path("README.md"),
    name="README File",
    description="Project README documentation",
    mime_type="text/markdown",
    tags={"documentation"}
)
mcp.add_resource(readme)

# Text resource
notice = TextResource(
    uri="resource://notice",
    name="Important Notice",
    text="System maintenance scheduled for tonight.",
    tags={"notification", "important"}
)
mcp.add_resource(notice)

# Directory listing
data_dir = DirectoryResource(
    uri="resource://data-files",
    path=Path("./data"),
    name="Data Directory",
    description="Lists all files in data directory",
    recursive=False
)
mcp.add_resource(data_dir)

# HTTP resource (requires httpx installed)
api_data = HttpResource(
    uri="resource://external-api",
    url="https://api.example.com/data",
    name="External API Data",
    description="Data from external API"
)
mcp.add_resource(api_data)
```

### Decorator Options

```python
@mcp.resource(
    uri="data://{id}",                    # Required: URI (may have parameters)
    name="Data Resource",                 # Human-readable name
    description="Custom description",     # Override docstring
    mime_type="application/json",         # Content type
    tags={"data", "public"},             # Categorization
    meta={"version": "2.1"},             # Custom metadata (v2.11.0+)
    version="2.1",                       # Version identifier (v3.0.0+)
    icons=[Icon(...)],                   # Visual icons (v2.13.0+)
    annotations={                        # MCP behavior hints
        "title": "Friendly Name",
        "readOnlyHint": True
    }
)
def get_data(id: str) -> str:
    """Resource with all decorator options."""
    import json
    return json.dumps({"id": id, "data": "value"})
```

### Context Access in Resources

```python
@mcp.resource("resource://system-status")
async def get_system_status(ctx: Context = CurrentContext()) -> str:
    """System status with context logging."""
    await ctx.info("Fetching system status")

    import json
    return json.dumps({
        "status": "operational",
        "request_id": ctx.request_id,
        "timestamp": "2026-02-09T12:00:00Z"
    })
```

### Error Handling (v2.4.1+)

```python
from fastmcp.exceptions import ResourceError

@mcp.resource("resource://data/{id}")
def get_data_by_id(id: str) -> str:
    """Fetch data with error handling."""
    if not id.isdigit():
        raise ResourceError("ID must be numeric")

    # Simulate database lookup
    if id == "999":
        raise ResourceError("Data not found for ID 999")

    import json
    return json.dumps({"id": id, "value": "data"})
```

---

## Prompts (@mcp.prompt)

Prompts are reusable message templates for structuring LLM interactions.

### Basic Prompts

```python
from fastmcp.prompts import Message

# Simple string prompt
@mcp.prompt
def ask_about_topic(topic: str) -> str:
    """Generate question about a topic."""
    return f"Can you please explain the concept of '{topic}' in detail?"

# Multi-turn conversation
@mcp.prompt
def code_review_request(language: str, code: str) -> list[Message]:
    """Generate code review conversation."""
    return [
        Message(
            f"Please review this {language} code for bugs and style issues:\n\n"
            f"```{language}\n{code}\n```"
        ),
        Message("I'll analyze the code now.", role="assistant"),
        Message("What specific aspects should I focus on?")
    ]
```

### Return Types

```python
from fastmcp.prompts import Message, PromptResult

# String → single user message
@mcp.prompt
def simple_prompt(topic: str) -> str:
    return f"Tell me about {topic}"

# List → multi-turn conversation
@mcp.prompt
def conversation(query: str) -> list[Message]:
    return [
        Message(f"User asks: {query}"),
        Message("Let me think about that.", role="assistant"),
        Message("Please provide more context.")
    ]

# PromptResult → full control (v3.0.0+)
@mcp.prompt
def advanced_prompt(task: str) -> PromptResult:
    return PromptResult(
        messages=[
            Message(f"Task: {task}"),
            Message("I'll help with that.", role="assistant")
        ],
        description="Custom description for this specific invocation",
        meta={"priority": "high", "category": "analysis"}
    )
```

### Message Class (v3.0.0+)

```python
from fastmcp.prompts import Message

# Default role: "user"
msg1 = Message("This is a user message")

# Explicit role
msg2 = Message("Assistant response here", role="assistant")

# Complex content (auto-serialized to JSON)
msg3 = Message({
    "type": "analysis_request",
    "data": [1, 2, 3],
    "options": {"detailed": True}
})

# Usage in prompt
@mcp.prompt
def structured_request(dataset: str) -> list[Message]:
    return [
        Message(f"Analyze dataset: {dataset}"),
        Message("I'll start the analysis.", role="assistant"),
        Message({
            "action": "request_additional_data",
            "fields": ["metadata", "schema"]
        })
    ]
```

### Complex Argument Types (v2.9.0+)

```python
@mcp.prompt
def analyze_data(
    numbers: list[int],
    metadata: dict[str, str],
    threshold: float,
    options: dict[str, bool] | None = None
) -> str:
    """Prompt with complex argument types."""
    avg = sum(numbers) / len(numbers) if numbers else 0
    above_threshold = avg > threshold

    return (
        f"Analyze this dataset:\n"
        f"- Numbers: {numbers}\n"
        f"- Average: {avg}\n"
        f"- Above threshold ({threshold}): {above_threshold}\n"
        f"- Metadata: {metadata}\n"
        f"- Options: {options or {}}"
    )
```

### Decorator Options

```python
@mcp.prompt(
    name="custom_prompt_name",           # Override function name
    title="Friendly Prompt Title",       # Human-readable title
    description="Custom description",     # Override docstring
    tags={"analysis", "data"},           # Categorization
    meta={"version": "1.1"},             # Custom metadata (v2.11.0+)
    version="1.1",                       # Version identifier (v3.0.0+)
    icons=[Icon(...)]                    # Visual icons (v2.13.0+)
)
def my_prompt(query: str) -> str:
    """Prompt with all decorator options."""
    return f"Process this query: {query}"
```

### Context Access in Prompts

```python
@mcp.prompt
async def contextual_prompt(
    topic: str,
    ctx: Context = CurrentContext()
) -> list[Message]:
    """Prompt with context access."""
    await ctx.info(f"Generating prompt for topic: {topic}")

    # Access resources
    guidelines = await ctx.read_resource("resource://guidelines")
    guidelines_text = guidelines[0].text if guidelines else ""

    return [
        Message(f"Discuss {topic} following these guidelines:\n\n{guidelines_text}"),
        Message("I'll follow those guidelines.", role="assistant")
    ]
```

---

## Validation Modes (v2.13.0+)

Control how FastMCP validates tool arguments:

```python
# Flexible validation (default) - coerce types when possible
mcp = FastMCP("FlexibleServer")
# LLM sends {"x": "123"} → converted to x=123 (int)

# Strict validation - reject type mismatches
mcp = FastMCP("StrictServer", strict_input_validation=True)
# LLM sends {"x": "123"} → error: expected int, got str
```

---

## Dynamic Management

Add, remove, and list components at runtime:

```python
# Add tool dynamically
def new_tool(x: int) -> int:
    """Dynamically added tool."""
    return x * 3

mcp.add_tool(new_tool)

# Remove tool
mcp.remove_tool("new_tool")

# List components
tools = mcp.list_tools()
resources = mcp.list_resources()
prompts = mcp.list_prompts()

# Add resource dynamically
from fastmcp.resources import TextResource

notice = TextResource(
    uri="resource://notice",
    name="Notice",
    text="Maintenance tonight"
)
mcp.add_resource(notice)
```

---

## Visibility Control (v3.0.0+)

Hide or show components at runtime:

```python
# Disable specific components
mcp.disable(keys={"tool:admin_delete", "resource://internal-config"})

# Disable by tags
mcp.disable(tags={"admin", "internal", "debug"})

# Enable only specific tags (hide everything else)
mcp.enable(tags={"public", "read-only"}, only=True)

# Reset visibility
mcp.reset_visibility()
```

**In tools (session-specific visibility)**:
```python
@mcp.tool
async def configure_session(
    mode: Literal["basic", "advanced"],
    ctx: Context = CurrentContext()
) -> str:
    """Configure session-specific tool visibility."""
    if mode == "basic":
        ctx.disable_components(tags={"advanced"})
    else:
        ctx.enable_components(tags={"advanced"})

    # Notify client of changes
    await ctx.send_notification(mcp.types.ToolListChangedNotification())

    return f"Session configured for {mode} mode"
```

---

## Method Registration

Register class methods as tools (automatically hides `self`):

```python
from fastmcp.tools import tool

class Calculator:
    def __init__(self, factor: int):
        self.factor = factor

    @tool()
    def multiply(self, x: int) -> int:
        """Multiply x by the calculator's factor."""
        return x * self.factor

calc = Calculator(factor=5)
mcp.add_tool(calc.multiply)

# LLM sees only 'x' parameter, not 'self'
```

---

## CLIO-Specific Integration

How CLIO Agent uses FastMCP for scientific tools:

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import CurrentContext, Depends
import h5py
import pandas as pd

# Scientific tool server
mcp = FastMCP("CLIOScientificTools")

# Database connection dependency
async def get_hdf5_file():
    """Open HDF5 file for scientific data."""
    f = h5py.File("/data/experiments.h5", "r")
    try:
        yield f
    finally:
        f.close()

@mcp.tool
async def read_dataset(
    dataset_path: str,
    start_row: int = 0,
    end_row: int | None = None,
    ctx: Context = CurrentContext(),
    hdf5_file = Depends(get_hdf5_file)
) -> dict:
    """Read dataset from HDF5 file with progress reporting."""
    await ctx.info(f"Reading dataset: {dataset_path}")

    dataset = hdf5_file[dataset_path]
    total_rows = dataset.shape[0]

    if end_row is None:
        end_row = total_rows

    await ctx.report_progress(0, end_row - start_row, "Reading data")

    data = dataset[start_row:end_row]

    await ctx.report_progress(end_row - start_row, end_row - start_row, "Complete")

    return {
        "shape": list(data.shape),
        "dtype": str(data.dtype),
        "rows_read": len(data),
        "preview": data[:5].tolist() if len(data) > 0 else []
    }

@mcp.resource("data://experiments/{experiment_id}/metadata")
def get_experiment_metadata(
    experiment_id: str,
    hdf5_file = Depends(get_hdf5_file)
) -> str:
    """Get metadata for an experiment."""
    import json
    group = hdf5_file[f"experiments/{experiment_id}"]
    metadata = dict(group.attrs)
    return json.dumps(metadata, indent=2)

if __name__ == "__main__":
    mcp.run()
```

**CLIO Integration Pattern**:
1. Scientific tools use `@mcp.tool` with rich type hints for data parameters
2. Context for progress reporting during long-running data operations
3. Dependencies for database connections, file handles, API clients
4. Resources for exposing metadata, schemas, data catalogs
5. Error handling with `ToolError` for domain-specific validation

---

## Complete Server Example

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import CurrentContext, Depends
from fastmcp.exceptions import ToolError, ResourceError
from fastmcp.prompts import Message
from typing import Annotated, Literal
from pydantic import Field, BaseModel
from contextlib import asynccontextmanager
import asyncio

# Lifespan for initialization/cleanup
@asynccontextmanager
async def app_lifespan(server):
    print("Server starting...")
    db_pool = await create_db_pool()
    try:
        yield {"db_pool": db_pool}
    finally:
        await db_pool.close()
        print("Server shutting down...")

# Initialize server
mcp = FastMCP(
    name="ComprehensiveServer",
    mask_error_details=True,
    strict_input_validation=False,
    on_duplicate_tools="warn",
    lifespan=app_lifespan
)

# Dependencies
def get_api_key() -> str:
    import os
    return os.environ.get("API_KEY", "default")

@asynccontextmanager
async def get_db(ctx: Context = CurrentContext()):
    pool = ctx.lifespan_context["db_pool"]
    async with pool.acquire() as conn:
        yield conn

# Tools
class DataModel(BaseModel):
    name: str
    values: list[float]

@mcp.tool(tags={"math", "basic"}, timeout=5.0)
def add(
    a: Annotated[int, Field(description="First number")],
    b: Annotated[int, Field(description="Second number")]
) -> int:
    """Add two integers."""
    return a + b

@mcp.tool(tags={"data", "analysis"})
async def analyze_data(
    data: DataModel,
    ctx: Context = CurrentContext()
) -> dict:
    """Analyze numerical data with progress reporting."""
    await ctx.info(f"Analyzing {len(data.values)} values")
    await ctx.report_progress(0, 100)

    avg = sum(data.values) / len(data.values) if data.values else 0

    await ctx.report_progress(100, 100)

    return {
        "name": data.name,
        "count": len(data.values),
        "average": avg,
        "min": min(data.values) if data.values else None,
        "max": max(data.values) if data.values else None
    }

@mcp.tool(tags={"database", "advanced"})
async def query_db(
    sql: str,
    api_key: str = Depends(get_api_key),
    db = Depends(get_db)
) -> list[dict]:
    """Execute SQL query with authentication."""
    if api_key != "valid-key":
        raise ToolError("Invalid API key")

    rows = await db.fetch(sql)
    return [dict(row) for row in rows]

# Resources
@mcp.resource("config://settings")
def get_config() -> str:
    """Application configuration."""
    import json
    return json.dumps({"version": "1.0", "mode": "production"})

@mcp.resource("user://{user_id}/profile{?include_metadata}")
async def get_user_profile(
    user_id: str,
    include_metadata: bool = False,
    db = Depends(get_db)
) -> str:
    """Get user profile by ID."""
    import json
    row = await db.fetchrow("SELECT * FROM users WHERE id = $1", int(user_id))
    if not row:
        raise ResourceError(f"User {user_id} not found")

    profile = dict(row)
    if not include_metadata:
        profile.pop("metadata", None)

    return json.dumps(profile)

# Prompts
@mcp.prompt(tags={"code"})
def code_review(language: str, code: str) -> list[Message]:
    """Generate code review prompt."""
    return [
        Message(f"Review this {language} code:\n\n```{language}\n{code}\n```"),
        Message("I'll review it for bugs and style.", role="assistant")
    ]

@mcp.prompt(tags={"analysis"})
async def data_analysis_prompt(
    dataset: str,
    ctx: Context = CurrentContext()
) -> str:
    """Generate data analysis prompt with context."""
    metadata = await ctx.read_resource(f"data://{dataset}/metadata")
    metadata_text = metadata[0].text if metadata else "No metadata available"

    return f"Analyze dataset '{dataset}'.\n\nMetadata:\n{metadata_text}"

if __name__ == "__main__":
    mcp.run()  # STDIO
    # mcp.run(transport="http", port=8000)  # HTTP server
```

---

This guide covers comprehensive FastMCP server creation patterns. For client usage, see `02_CLIENT_USAGE.md`.
