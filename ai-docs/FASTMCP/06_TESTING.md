# FastMCP Testing Guide
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Comprehensive guide to testing FastMCP servers, tools, resources, and prompts using in-memory clients and pytest.

---

## Table of Contents

1. [Testing Philosophy](#testing-philosophy)
2. [In-Memory Testing with Client(server)](#in-memory-testing-with-clientserver)
3. [Testing Tools](#testing-tools)
4. [Testing Resources](#testing-resources)
5. [Testing Prompts](#testing-prompts)
6. [Async Testing with pytest-asyncio](#async-testing-with-pytest-asyncio)
7. [Mocking Dependencies](#mocking-dependencies)
8. [Integration Testing Patterns](#integration-testing-patterns)
9. [Testing Composed/Mounted Servers](#testing-composedmounted-servers)
10. [CLIO-Specific Testing Patterns](#clio-specific-testing-patterns)

---

## Testing Philosophy

**Key Principle**: FastMCP testing does NOT require network connections. Use `Client(server)` for in-memory testing.

```python
from fastmcp import FastMCP, Client

# Create server
mcp = FastMCP("my-server")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

# Test WITHOUT network - Client(server) creates in-memory connection
async def test_add():
    async with Client(mcp) as client:
        result = await client.call_tool("add", {"a": 5, "b": 3})
        assert result == 8
```

**Benefits**:
- **Fast**: No network overhead
- **Isolated**: No external dependencies
- **Deterministic**: No flaky network tests
- **Simple**: No server startup/teardown

---

## In-Memory Testing with Client(server)

### Basic Pattern

```python
import pytest
from fastmcp import FastMCP, Client

@pytest.fixture
def mcp_server():
    """Create test server."""
    mcp = FastMCP("test-server")

    @mcp.tool()
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    return mcp


@pytest.mark.asyncio
async def test_greet_tool(mcp_server):
    """Test greet tool via in-memory client."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("greet", {"name": "Alice"})
        assert result == "Hello, Alice!"
```

### Client Context Manager

The `Client(server)` context manager:
1. Creates in-memory connection to server
2. Initializes client session
3. Provides async tool/resource/prompt methods
4. Cleans up on exit

```python
async with Client(mcp_server) as client:
    # Client is ready
    tools = await client.list_tools()
    result = await client.call_tool("tool_name", params)
    # Cleanup happens automatically
```

---

## Testing Tools

### Basic Tool Testing

```python
from fastmcp import FastMCP, Client
import pytest

@pytest.fixture
def calculator_server():
    mcp = FastMCP("calculator")

    @mcp.tool()
    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    @mcp.tool()
    def divide(a: float, b: float) -> float:
        """Divide a by b."""
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b

    return mcp


@pytest.mark.asyncio
async def test_multiply(calculator_server):
    """Test multiplication tool."""
    async with Client(calculator_server) as client:
        result = await client.call_tool("multiply", {"a": 6, "b": 7})
        assert result == 42


@pytest.mark.asyncio
async def test_divide_success(calculator_server):
    """Test division with valid inputs."""
    async with Client(calculator_server) as client:
        result = await client.call_tool("divide", {"a": 10.0, "b": 2.0})
        assert result == 5.0


@pytest.mark.asyncio
async def test_divide_zero_error(calculator_server):
    """Test division by zero raises error."""
    async with Client(calculator_server) as client:
        with pytest.raises(ValueError, match="Cannot divide by zero"):
            await client.call_tool("divide", {"a": 10.0, "b": 0.0})
```

### Testing Tool Listing

```python
@pytest.mark.asyncio
async def test_list_tools(calculator_server):
    """Test tool discovery."""
    async with Client(calculator_server) as client:
        tools = await client.list_tools()

        # Check tool names
        tool_names = {t.name for t in tools}
        assert tool_names == {"multiply", "divide"}

        # Check tool descriptions
        multiply_tool = next(t for t in tools if t.name == "multiply")
        assert "Multiply two numbers" in multiply_tool.description
```

### Testing Tool Schemas

```python
@pytest.mark.asyncio
async def test_tool_schema(calculator_server):
    """Test tool parameter schemas."""
    async with Client(calculator_server) as client:
        tools = await client.list_tools()
        divide_tool = next(t for t in tools if t.name == "divide")

        # Check input schema
        params = divide_tool.inputSchema
        assert "a" in params["properties"]
        assert "b" in params["properties"]
        assert params["properties"]["a"]["type"] == "number"
```

---

## Testing Resources

### Basic Resource Testing

```python
from fastmcp import FastMCP, Client, Resource
import pytest

@pytest.fixture
def config_server():
    mcp = FastMCP("config-server")

    @mcp.resource("config://app/settings")
    def get_settings() -> str:
        """Application settings."""
        return "debug=true\nport=8080"

    @mcp.resource("config://app/version")
    def get_version() -> str:
        """Application version."""
        return "1.0.0"

    return mcp


@pytest.mark.asyncio
async def test_read_settings_resource(config_server):
    """Test reading settings resource."""
    async with Client(config_server) as client:
        content = await client.read_resource("config://app/settings")
        assert "debug=true" in content
        assert "port=8080" in content


@pytest.mark.asyncio
async def test_read_version_resource(config_server):
    """Test reading version resource."""
    async with Client(config_server) as client:
        version = await client.read_resource("config://app/version")
        assert version == "1.0.0"
```

### Testing Dynamic Resources

```python
@pytest.fixture
def file_server():
    mcp = FastMCP("file-server")

    @mcp.resource("file://{path}")
    def read_file(path: str) -> str:
        """Read file by path."""
        # Simulate file reading (in real server, would read actual files)
        files = {
            "data.txt": "Sample data",
            "config.json": '{"key": "value"}',
        }
        if path not in files:
            raise FileNotFoundError(f"File not found: {path}")
        return files[path]

    return mcp


@pytest.mark.asyncio
async def test_read_existing_file(file_server):
    """Test reading existing file."""
    async with Client(file_server) as client:
        content = await client.read_resource("file://data.txt")
        assert content == "Sample data"


@pytest.mark.asyncio
async def test_read_missing_file(file_server):
    """Test reading missing file raises error."""
    async with Client(file_server) as client:
        with pytest.raises(FileNotFoundError):
            await client.read_resource("file://missing.txt")
```

### Testing Resource Listing

```python
@pytest.mark.asyncio
async def test_list_resources(config_server):
    """Test resource discovery."""
    async with Client(config_server) as client:
        resources = await client.list_resources()

        # Check resource URIs
        uris = {r.uri for r in resources}
        assert "config://app/settings" in uris
        assert "config://app/version" in uris
```

---

## Testing Prompts

### Basic Prompt Testing

```python
from fastmcp import FastMCP, Client
import pytest

@pytest.fixture
def prompt_server():
    mcp = FastMCP("prompt-server")

    @mcp.prompt()
    def code_review(language: str, code: str) -> str:
        """Generate code review prompt."""
        return f"""Review this {language} code:

```{language}
{code}
```

Provide feedback on:
1. Code quality
2. Potential bugs
3. Best practices
"""

    return mcp


@pytest.mark.asyncio
async def test_code_review_prompt(prompt_server):
    """Test code review prompt generation."""
    async with Client(prompt_server) as client:
        prompt = await client.get_prompt(
            "code_review",
            {"language": "python", "code": "def add(a, b):\n    return a + b"}
        )

        assert "python" in prompt.lower()
        assert "def add(a, b)" in prompt
        assert "Code quality" in prompt


@pytest.mark.asyncio
async def test_list_prompts(prompt_server):
    """Test prompt discovery."""
    async with Client(prompt_server) as client:
        prompts = await client.list_prompts()

        prompt_names = {p.name for p in prompts}
        assert "code_review" in prompt_names
```

---

## Async Testing with pytest-asyncio

### Setup pytest-asyncio

Install dependency:
```bash
pip install pytest-asyncio
```

Configure in `pytest.ini` or `pyproject.toml`:
```ini
[pytest]
asyncio_mode = auto
```

Or use `@pytest.mark.asyncio` decorator on each test.

### Testing Async Operations

```python
import pytest
import asyncio
from fastmcp import FastMCP, Client

@pytest.fixture
def async_server():
    mcp = FastMCP("async-server")

    @mcp.tool()
    async def slow_operation(duration: float) -> str:
        """Simulate slow async operation."""
        await asyncio.sleep(duration)
        return f"Completed after {duration}s"

    return mcp


@pytest.mark.asyncio
async def test_slow_operation(async_server):
    """Test async tool execution."""
    async with Client(async_server) as client:
        result = await client.call_tool("slow_operation", {"duration": 0.1})
        assert "Completed after 0.1s" == result


@pytest.mark.asyncio
async def test_concurrent_operations(async_server):
    """Test concurrent tool calls."""
    async with Client(async_server) as client:
        # Execute multiple operations concurrently
        results = await asyncio.gather(
            client.call_tool("slow_operation", {"duration": 0.1}),
            client.call_tool("slow_operation", {"duration": 0.1}),
            client.call_tool("slow_operation", {"duration": 0.1}),
        )

        assert len(results) == 3
        assert all("Completed" in r for r in results)
```

---

## Mocking Dependencies

### Using Depends for Testable Dependencies

```python
from fastmcp import FastMCP, Client, Depends
import pytest
from typing import Protocol

# Define dependency protocol
class Database(Protocol):
    def get_user(self, user_id: int) -> dict: ...

# Production database
class ProdDatabase:
    def get_user(self, user_id: int) -> dict:
        # Real database query
        raise NotImplementedError("Production DB")

# Mock database for testing
class MockDatabase:
    def __init__(self, users: dict[int, dict]):
        self.users = users

    def get_user(self, user_id: int) -> dict:
        if user_id not in self.users:
            raise ValueError(f"User {user_id} not found")
        return self.users[user_id]


# Create server with dependency
def create_user_server(db: Database):
    mcp = FastMCP("user-server")

    @mcp.tool()
    def get_user_name(user_id: int, db: Database = Depends(lambda: db)) -> str:
        """Get user name by ID."""
        user = db.get_user(user_id)
        return user["name"]

    return mcp


@pytest.fixture
def mock_db():
    """Create mock database."""
    return MockDatabase(users={
        1: {"name": "Alice", "email": "alice@example.com"},
        2: {"name": "Bob", "email": "bob@example.com"},
    })


@pytest.fixture
def user_server(mock_db):
    """Create server with mock database."""
    return create_user_server(mock_db)


@pytest.mark.asyncio
async def test_get_existing_user(user_server):
    """Test getting existing user."""
    async with Client(user_server) as client:
        name = await client.call_tool("get_user_name", {"user_id": 1})
        assert name == "Alice"


@pytest.mark.asyncio
async def test_get_missing_user(user_server):
    """Test getting missing user raises error."""
    async with Client(user_server) as client:
        with pytest.raises(ValueError, match="User 999 not found"):
            await client.call_tool("get_user_name", {"user_id": 999})
```

---

## Integration Testing Patterns

### Testing Real File Operations

```python
import pytest
import tempfile
from pathlib import Path
from fastmcp import FastMCP, Client

@pytest.fixture
def temp_dir():
    """Create temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def file_ops_server(temp_dir):
    """Create server that operates on real files."""
    mcp = FastMCP("file-ops")

    @mcp.tool()
    def write_file(filename: str, content: str) -> str:
        """Write content to file."""
        path = temp_dir / filename
        path.write_text(content)
        return f"Written {len(content)} bytes to {filename}"

    @mcp.tool()
    def read_file(filename: str) -> str:
        """Read file content."""
        path = temp_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"File not found: {filename}")
        return path.read_text()

    return mcp


@pytest.mark.asyncio
async def test_write_and_read_file(file_ops_server, temp_dir):
    """Integration test: write then read file."""
    async with Client(file_ops_server) as client:
        # Write file
        write_result = await client.call_tool(
            "write_file",
            {"filename": "test.txt", "content": "Hello, World!"}
        )
        assert "Written 13 bytes" in write_result

        # Read file back
        content = await client.call_tool("read_file", {"filename": "test.txt"})
        assert content == "Hello, World!"

        # Verify file exists on disk
        assert (temp_dir / "test.txt").exists()
```

---

## Testing Composed/Mounted Servers

### Testing Server Composition

```python
import pytest
from fastmcp import FastMCP, Client

@pytest.fixture
def auth_server():
    """Authentication server."""
    mcp = FastMCP("auth")

    @mcp.tool()
    def login(username: str, password: str) -> str:
        """Login user."""
        if username == "admin" and password == "secret":
            return "token_abc123"
        raise ValueError("Invalid credentials")

    return mcp


@pytest.fixture
def api_server():
    """API server."""
    mcp = FastMCP("api")

    @mcp.tool()
    def get_data(token: str) -> dict:
        """Get data with auth token."""
        if token != "token_abc123":
            raise ValueError("Invalid token")
        return {"data": "sensitive info"}

    return mcp


@pytest.fixture
def gateway_server(auth_server, api_server):
    """Gateway server mounting auth and api."""
    gateway = FastMCP("gateway")
    gateway.mount("/auth", auth_server)
    gateway.mount("/api", api_server)
    return gateway


@pytest.mark.asyncio
async def test_composed_auth_flow(gateway_server):
    """Test authentication flow through gateway."""
    async with Client(gateway_server) as client:
        # Login through auth server
        token = await client.call_tool("login", {
            "username": "admin",
            "password": "secret"
        })
        assert token == "token_abc123"

        # Use token to access API server
        data = await client.call_tool("get_data", {"token": token})
        assert data["data"] == "sensitive info"


@pytest.mark.asyncio
async def test_list_composed_tools(gateway_server):
    """Test tool listing from composed server."""
    async with Client(gateway_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        # Tools from both servers should be available
        assert "login" in tool_names
        assert "get_data" in tool_names
```

---

## CLIO-Specific Testing Patterns

### Testing Scientific Tool Servers

```python
import pytest
import h5py
import tempfile
from pathlib import Path
from fastmcp import FastMCP, Client

@pytest.fixture
def sample_hdf5_file(tmp_path):
    """Create sample HDF5 file for testing."""
    file_path = tmp_path / "sample.h5"
    with h5py.File(file_path, "w") as f:
        f.create_dataset("temperature", data=[20.5, 21.0, 22.5, 23.0])
        f.create_dataset("pressure", data=[101.3, 101.4, 101.2, 101.5])
    return file_path


@pytest.fixture
def hdf5_server():
    """HDF5 analysis server for CLIO."""
    mcp = FastMCP("hdf5-analysis")

    @mcp.tool()
    def analyze_dataset(filepath: str, dataset_name: str) -> dict:
        """Analyze HDF5 dataset statistics."""
        import numpy as np

        with h5py.File(filepath, "r") as f:
            if dataset_name not in f:
                raise ValueError(f"Dataset not found: {dataset_name}")

            data = f[dataset_name][:]
            return {
                "mean": float(np.mean(data)),
                "std": float(np.std(data)),
                "min": float(np.min(data)),
                "max": float(np.max(data)),
                "shape": list(data.shape),
            }

    @mcp.resource("hdf5://{filepath}")
    def list_datasets(filepath: str) -> str:
        """List datasets in HDF5 file."""
        with h5py.File(filepath, "r") as f:
            datasets = list(f.keys())
        return "\n".join(datasets)

    return mcp


@pytest.mark.asyncio
async def test_hdf5_analyze_temperature(hdf5_server, sample_hdf5_file):
    """Test HDF5 dataset analysis."""
    async with Client(hdf5_server) as client:
        stats = await client.call_tool("analyze_dataset", {
            "filepath": str(sample_hdf5_file),
            "dataset_name": "temperature"
        })

        assert stats["mean"] == pytest.approx(21.75, rel=0.01)
        assert stats["shape"] == [4]
        assert stats["min"] == 20.5
        assert stats["max"] == 23.0


@pytest.mark.asyncio
async def test_hdf5_list_datasets(hdf5_server, sample_hdf5_file):
    """Test listing HDF5 datasets via resource."""
    async with Client(hdf5_server) as client:
        content = await client.read_resource(f"hdf5://{sample_hdf5_file}")

        datasets = content.strip().split("\n")
        assert "temperature" in datasets
        assert "pressure" in datasets
```

### Testing CLIO Gateway Pattern

```python
@pytest.fixture
def clio_gateway(hdf5_server):
    """CLIO gateway aggregating scientific tool servers."""
    gateway = FastMCP("clio-gateway")

    # Mount scientific tool servers
    gateway.mount("/hdf5", hdf5_server)
    # In production, would mount more servers:
    # gateway.mount("/parquet", parquet_server)
    # gateway.mount("/netcdf", netcdf_server)

    # Add gateway-level coordination tools
    @gateway.tool()
    def analyze_scientific_data(filepath: str, format: str) -> dict:
        """Route analysis to appropriate server based on format."""
        # Routing logic (simplified)
        if format == "hdf5":
            return {"status": "routed to hdf5 server", "filepath": filepath}
        raise ValueError(f"Unsupported format: {format}")

    return gateway


@pytest.mark.asyncio
async def test_clio_gateway_routing(clio_gateway, sample_hdf5_file):
    """Test CLIO gateway routes to correct backend."""
    async with Client(clio_gateway) as client:
        # Test gateway-level tool
        result = await client.call_tool("analyze_scientific_data", {
            "filepath": str(sample_hdf5_file),
            "format": "hdf5"
        })
        assert result["status"] == "routed to hdf5 server"

        # Test direct access to mounted HDF5 server
        stats = await client.call_tool("analyze_dataset", {
            "filepath": str(sample_hdf5_file),
            "dataset_name": "temperature"
        })
        assert "mean" in stats


@pytest.mark.asyncio
async def test_clio_gateway_tool_discovery(clio_gateway):
    """Test CLIO gateway exposes tools from all mounted servers."""
    async with Client(clio_gateway) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        # Gateway tool
        assert "analyze_scientific_data" in tool_names

        # HDF5 server tool
        assert "analyze_dataset" in tool_names
```

### Testing with ARC Memory Integration

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ToolInvocation:
    """Tool invocation record for ARC."""
    tool_name: str
    params: dict
    result: any
    duration_ms: float
    timestamp: datetime


class MockARCMemory:
    """Mock ARC memory for testing."""
    def __init__(self):
        self.invocations: list[ToolInvocation] = []
        self.cache: dict[str, any] = {}

    def cache_tool_result(self, tool_name: str, params: dict, result: any):
        """Cache tool result."""
        cache_key = f"{tool_name}:{hash(str(params))}"
        self.cache[cache_key] = result

    def get_cached_result(self, tool_name: str, params: dict) -> any | None:
        """Get cached result."""
        cache_key = f"{tool_name}:{hash(str(params))}"
        return self.cache.get(cache_key)

    def store_invocation(self, invocation: ToolInvocation):
        """Store invocation record."""
        self.invocations.append(invocation)


@pytest.fixture
def arc_enabled_server():
    """Server with ARC memory integration."""
    arc = MockARCMemory()
    mcp = FastMCP("arc-server")

    @mcp.tool()
    def expensive_computation(n: int) -> int:
        """Expensive computation with ARC caching."""
        # Check cache
        cached = arc.get_cached_result("expensive_computation", {"n": n})
        if cached is not None:
            return cached

        # Compute result
        import time
        start = time.time()
        result = sum(range(n))
        duration_ms = (time.time() - start) * 1000

        # Store in ARC
        arc.cache_tool_result("expensive_computation", {"n": n}, result)
        arc.store_invocation(ToolInvocation(
            tool_name="expensive_computation",
            params={"n": n},
            result=result,
            duration_ms=duration_ms,
            timestamp=datetime.now()
        ))

        return result

    # Expose ARC for testing
    mcp._arc = arc
    return mcp


@pytest.mark.asyncio
async def test_arc_caching(arc_enabled_server):
    """Test ARC caching improves performance."""
    async with Client(arc_enabled_server) as client:
        # First call - cache miss
        result1 = await client.call_tool("expensive_computation", {"n": 10000})
        assert result1 == sum(range(10000))

        # Second call - cache hit
        result2 = await client.call_tool("expensive_computation", {"n": 10000})
        assert result2 == result1

        # Verify ARC stored invocations
        arc = arc_enabled_server._arc
        assert len(arc.invocations) == 1  # Only first call stored

        # Verify cache hit
        cached = arc.get_cached_result("expensive_computation", {"n": 10000})
        assert cached == result1
```

---

## Summary

**Key Testing Patterns**:

1. **In-Memory Testing**: Use `Client(server)` for fast, isolated tests
2. **Async Testing**: Use `pytest-asyncio` for async operations
3. **Dependency Injection**: Use `Depends` for mockable dependencies
4. **Integration Testing**: Test with real files in temp directories
5. **Composed Servers**: Test gateway/mounted server patterns
6. **CLIO Patterns**: Test scientific tool servers with HDF5/Parquet/NetCDF
7. **ARC Integration**: Test caching and performance optimization

**Best Practices**:
- Always use fixtures for server creation
- Test success cases AND error cases
- Verify tool/resource/prompt schemas
- Test concurrent operations for async tools
- Mock external dependencies (databases, APIs)
- Use temporary directories for file operations
- Test cache behavior for performance-critical tools

FastMCP's in-memory testing makes it easy to write fast, reliable tests for complex MCP server ecosystems like CLIO Agent's scientific tool infrastructure.
