# FastMCP Transforms
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

Transforms are a powerful FastMCP feature that allows you to modify server capabilities before exposing them to clients. They enable filtering, customization, and behavioral modification of tools, resources, and prompts.

## Table of Contents
- [Transform Concept](#transform-concept)
- [Built-in Transforms](#built-in-transforms)
- [Namespace Transform](#namespace-transform)
- [ToolTransform](#tooltransform)
- [Tool.from_tool()](#toolfrom_tool)
- [Enabled Transform](#enabled-transform)
- [Custom Transform Functions](#custom-transform-functions)
- [Transform Chaining](#transform-chaining)
- [Custom Output Schemas](#custom-output-schemas)
- [CLIO-Specific: Capability Filtering](#clio-specific-capability-filtering)

## Transform Concept

Transforms modify server capabilities before they are exposed to clients. They operate at the server composition layer, allowing you to:

- Add prefixes to tool/resource names
- Conditionally enable/disable capabilities
- Wrap tool execution with custom logic
- Modify tool schemas and descriptions
- Filter capabilities based on context
- Add authentication/authorization checks

Transforms are applied when mounting or importing servers.

## Built-in Transforms

FastMCP provides several built-in transforms:

1. **Namespace** - Prefix all tool/resource names
2. **ToolTransform** - Modify tool behavior and schema
3. **Enabled** - Conditionally enable tools
4. **ResourceTransform** - Modify resource behavior (similar to ToolTransform)

## Namespace Transform

The Namespace transform automatically prefixes all capabilities from a server.

### Basic Namespace

```python
from fastmcp import FastMCP
from fastmcp.transforms import Namespace

# Create source server
source_server = FastMCP(name="Source")

@source_server.tool()
def calculate(x: float, y: float) -> float:
    """Perform calculation."""
    return x + y

@source_server.resource("data://{id}")
def get_data(id: str) -> str:
    """Get data resource."""
    return f"Data {id}"

# Apply namespace transform
gateway = FastMCP(name="Gateway")

# Mount with namespace transform
gateway.mount(
    source_server,
    transforms=[Namespace(prefix="math")]
)

# Tools and resources are now:
# - math_calculate(x, y)
# - math_data://{id}
```

### Custom Separator

```python
from fastmcp import FastMCP
from fastmcp.transforms import Namespace

source_server = FastMCP(name="Source")

@source_server.tool()
def my_tool() -> str:
    return "result"

gateway = FastMCP(name="Gateway")

# Use dot separator instead of underscore
gateway.mount(
    source_server,
    transforms=[Namespace(prefix="source", separator=".")]
)

# Tool name becomes: source.my_tool
```

### Namespace with Multiple Servers

```python
from fastmcp import FastMCP
from fastmcp.transforms import Namespace

server_a = FastMCP(name="Server A")
server_b = FastMCP(name="Server B")

@server_a.tool()
def process(data: str) -> str:
    return f"A: {data}"

@server_b.tool()
def process(data: str) -> str:
    return f"B: {data}"

gateway = FastMCP(name="Gateway")

# Apply different namespaces to avoid collision
gateway.mount(server_a, transforms=[Namespace(prefix="a")])
gateway.mount(server_b, transforms=[Namespace(prefix="b")])

# Tools: a_process() and b_process()
```

## ToolTransform

ToolTransform allows you to modify tool behavior, schema, and execution.

### Basic ToolTransform

```python
from fastmcp import FastMCP
from fastmcp.transforms import ToolTransform
from fastmcp import Context

# Create source server
source_server = FastMCP(name="Source")

@source_server.tool()
def sensitive_operation(password: str) -> str:
    """Perform sensitive operation."""
    return f"Executed with: {password}"

# Create transform to add logging
class LoggingTransform(ToolTransform):
    async def execute(self, ctx: Context, **kwargs):
        """Wrap execution with logging."""
        await ctx.info(f"Tool {self.tool.name} called")

        # Call original tool
        result = await self.tool.execute(ctx, **kwargs)

        await ctx.info(f"Tool {self.tool.name} completed")
        return result

gateway = FastMCP(name="Gateway")
gateway.mount(source_server, transforms=[LoggingTransform()])
```

### ToolTransform with Schema Modification

```python
from fastmcp import FastMCP
from fastmcp.transforms import ToolTransform
from fastmcp import Context

source_server = FastMCP(name="Source")

@source_server.tool()
def calculate(x: float, y: float) -> float:
    """Basic calculation."""
    return x + y

# Transform to enhance documentation
class DocumentationTransform(ToolTransform):
    def transform_schema(self, schema: dict) -> dict:
        """Enhance tool schema."""
        schema = schema.copy()
        schema["description"] = (
            f"[ENHANCED] {schema.get('description', '')}\n\n"
            "This tool has been validated and enhanced."
        )
        return schema

gateway = FastMCP(name="Gateway")
gateway.mount(source_server, transforms=[DocumentationTransform()])
```

### ToolTransform for Validation

```python
from fastmcp import FastMCP
from fastmcp.transforms import ToolTransform
from fastmcp import Context

source_server = FastMCP(name="Source")

@source_server.tool()
def divide(x: float, y: float) -> float:
    """Divide two numbers."""
    return x / y

# Transform to add validation
class ValidationTransform(ToolTransform):
    async def execute(self, ctx: Context, **kwargs):
        """Validate inputs before execution."""
        # Add validation logic
        if "y" in kwargs and kwargs["y"] == 0:
            raise ValueError("Division by zero not allowed")

        # Call original tool
        return await self.tool.execute(ctx, **kwargs)

gateway = FastMCP(name="Gateway")
gateway.mount(source_server, transforms=[ValidationTransform()])
```

## Tool.from_tool()

`Tool.from_tool()` creates a derived tool with custom logic while preserving the original tool's schema.

### Basic Derivation

```python
from fastmcp import FastMCP, Context, Tool

source_server = FastMCP(name="Source")

@source_server.tool()
def base_operation(data: str) -> str:
    """Base operation."""
    return data.upper()

# Create derived tool with modified behavior
async def enhanced_operation(data: str, ctx: Context) -> str:
    """Enhanced version with logging."""
    await ctx.info("Enhanced operation called")

    # Get original tool
    original = source_server.get_tool("base_operation")

    # Call original
    result = await original.execute(ctx, data=data)

    # Add enhancement
    return f"ENHANCED: {result}"

# Create new tool from original
enhanced_tool = Tool.from_tool(
    source_server.get_tool("base_operation"),
    fn=enhanced_operation,
    name="enhanced_operation"
)

# Add to gateway
gateway = FastMCP(name="Gateway")
gateway.add_tool(enhanced_tool)
```

### Wrapping Multiple Tools

```python
from fastmcp import FastMCP, Context, Tool

source_server = FastMCP(name="Source")

@source_server.tool()
def tool_a(x: int) -> int:
    return x * 2

@source_server.tool()
def tool_b(x: int) -> int:
    return x + 10

# Create wrapped versions with timing
def create_timed_tool(original_tool: Tool) -> Tool:
    async def timed_wrapper(**kwargs) -> dict:
        import time
        start = time.time()

        result = await original_tool.execute(**kwargs)

        elapsed = time.time() - start
        return {
            "result": result,
            "elapsed_ms": elapsed * 1000
        }

    return Tool.from_tool(
        original_tool,
        fn=timed_wrapper,
        name=f"timed_{original_tool.name}"
    )

# Apply to all tools
gateway = FastMCP(name="Gateway")
for tool in source_server.list_tools():
    timed_tool = create_timed_tool(tool)
    gateway.add_tool(timed_tool)
```

## Enabled Transform

The Enabled transform conditionally enables tools based on runtime conditions.

### Basic Conditional Enabling

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import Enabled

source_server = FastMCP(name="Source")

@source_server.tool()
def admin_operation(action: str) -> str:
    """Admin-only operation."""
    return f"Executed: {action}"

@source_server.tool()
def public_operation(query: str) -> str:
    """Public operation."""
    return f"Result: {query}"

# Enable admin tools only for admin users
def is_admin_user(ctx: Context) -> bool:
    """Check if current user is admin."""
    # In production, check ctx.client_id or custom headers
    return ctx.client_id == "admin_client"

gateway = FastMCP(name="Gateway")

# Apply Enabled transform to admin tools
gateway.mount(
    source_server,
    transforms=[
        Enabled(
            condition=is_admin_user,
            tools=["admin_operation"]  # Only affect these tools
        )
    ]
)

# admin_operation is only available when is_admin_user returns True
# public_operation is always available
```

### Environment-Based Enabling

```python
import os
from fastmcp import FastMCP, Context
from fastmcp.transforms import Enabled

source_server = FastMCP(name="Source")

@source_server.tool()
def experimental_feature(data: str) -> str:
    """Experimental feature."""
    return f"Experimental: {data}"

# Enable experimental tools only in development
def is_development() -> bool:
    return os.getenv("ENVIRONMENT") == "development"

gateway = FastMCP(name="Gateway")
gateway.mount(
    source_server,
    transforms=[
        Enabled(
            condition=is_development,
            tools=["experimental_feature"]
        )
    ]
)
```

### Feature Flag Transform

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import Enabled

class FeatureFlagTransform(Enabled):
    """Enable tools based on feature flags."""

    def __init__(self, flags: dict[str, bool]):
        self.flags = flags

    def is_enabled(self, tool_name: str, ctx: Context) -> bool:
        """Check if tool is enabled via feature flag."""
        return self.flags.get(tool_name, True)

source_server = FastMCP(name="Source")

@source_server.tool()
def new_feature_a() -> str:
    return "Feature A"

@source_server.tool()
def new_feature_b() -> str:
    return "Feature B"

# Configure feature flags
feature_flags = {
    "new_feature_a": True,   # Enabled
    "new_feature_b": False,  # Disabled
}

gateway = FastMCP(name="Gateway")
gateway.mount(
    source_server,
    transforms=[FeatureFlagTransform(flags=feature_flags)]
)
```

## Custom Transform Functions

Create custom transforms for specialized behaviors.

### Authentication Transform

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import ToolTransform

class AuthenticationTransform(ToolTransform):
    """Require authentication for all tools."""

    def __init__(self, required_token: str):
        self.required_token = required_token

    async def execute(self, ctx: Context, **kwargs):
        """Verify authentication before execution."""
        # Check for auth token in context
        # In production, parse from ctx.client_id or custom headers
        provided_token = kwargs.pop("auth_token", None)

        if provided_token != self.required_token:
            raise PermissionError("Invalid authentication token")

        # Execute original tool
        return await self.tool.execute(ctx, **kwargs)

    def transform_schema(self, schema: dict) -> dict:
        """Add auth_token parameter to schema."""
        schema = schema.copy()
        schema["parameters"]["properties"]["auth_token"] = {
            "type": "string",
            "description": "Authentication token"
        }
        schema["parameters"]["required"] = (
            schema["parameters"].get("required", []) + ["auth_token"]
        )
        return schema

source_server = FastMCP(name="Source")

@source_server.tool()
def protected_operation(data: str) -> str:
    return f"Protected: {data}"

gateway = FastMCP(name="Gateway")
gateway.mount(
    source_server,
    transforms=[AuthenticationTransform(required_token="secret123")]
)

# Tool now requires auth_token parameter
```

### Rate Limiting Transform

```python
import time
from fastmcp import FastMCP, Context
from fastmcp.transforms import ToolTransform

class RateLimitTransform(ToolTransform):
    """Rate limit tool executions."""

    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: dict[str, list[float]] = {}

    async def execute(self, ctx: Context, **kwargs):
        """Check rate limit before execution."""
        client_id = ctx.client_id or "anonymous"
        now = time.time()

        # Initialize or clean old calls
        if client_id not in self.calls:
            self.calls[client_id] = []

        self.calls[client_id] = [
            t for t in self.calls[client_id]
            if now - t < self.window_seconds
        ]

        # Check rate limit
        if len(self.calls[client_id]) >= self.max_calls:
            raise RuntimeError(
                f"Rate limit exceeded: {self.max_calls} calls "
                f"per {self.window_seconds}s"
            )

        # Record this call
        self.calls[client_id].append(now)

        # Execute original tool
        return await self.tool.execute(ctx, **kwargs)

source_server = FastMCP(name="Source")

@source_server.tool()
def expensive_operation(data: str) -> str:
    """Expensive operation."""
    return f"Processed: {data}"

gateway = FastMCP(name="Gateway")
gateway.mount(
    source_server,
    transforms=[RateLimitTransform(max_calls=10, window_seconds=60)]
)

# Tool limited to 10 calls per 60 seconds per client
```

### Caching Transform

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import ToolTransform
import hashlib
import json

class CachingTransform(ToolTransform):
    """Cache tool results."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self.cache: dict[str, tuple[float, any]] = {}

    def _cache_key(self, tool_name: str, kwargs: dict) -> str:
        """Generate cache key from tool name and args."""
        key_data = {"tool": tool_name, "kwargs": kwargs}
        key_json = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_json.encode()).hexdigest()

    async def execute(self, ctx: Context, **kwargs):
        """Check cache before execution."""
        import time

        cache_key = self._cache_key(self.tool.name, kwargs)
        now = time.time()

        # Check cache
        if cache_key in self.cache:
            cached_time, cached_result = self.cache[cache_key]
            if now - cached_time < self.ttl_seconds:
                await ctx.info(f"Cache hit for {self.tool.name}")
                return cached_result

        # Execute original tool
        result = await self.tool.execute(ctx, **kwargs)

        # Store in cache
        self.cache[cache_key] = (now, result)

        return result

source_server = FastMCP(name="Source")

@source_server.tool()
async def slow_computation(x: int, ctx: Context) -> int:
    """Slow computation."""
    await ctx.info("Computing...")
    import asyncio
    await asyncio.sleep(2)  # Simulate slow operation
    return x ** 2

gateway = FastMCP(name="Gateway")
gateway.mount(
    source_server,
    transforms=[CachingTransform(ttl_seconds=60)]
)

# Results cached for 60 seconds
```

## Transform Chaining

Multiple transforms can be chained together for complex behaviors.

### Chaining Example

```python
from fastmcp import FastMCP
from fastmcp.transforms import Namespace, ToolTransform

source_server = FastMCP(name="Source")

@source_server.tool()
def my_tool(data: str) -> str:
    return data

gateway = FastMCP(name="Gateway")

# Apply multiple transforms in order
gateway.mount(
    source_server,
    transforms=[
        Namespace(prefix="source"),          # First: add prefix
        LoggingTransform(),                  # Second: add logging
        ValidationTransform(),               # Third: validate inputs
        CachingTransform(ttl_seconds=300),  # Fourth: cache results
    ]
)

# Transforms applied in order: namespace -> logging -> validation -> caching
```

### Transform Pipeline

```python
from fastmcp import FastMCP
from fastmcp.transforms import ToolTransform

def create_transform_pipeline(*transforms: ToolTransform) -> list[ToolTransform]:
    """Create a reusable transform pipeline."""
    return list(transforms)

# Define standard pipeline
standard_pipeline = create_transform_pipeline(
    LoggingTransform(),
    ValidationTransform(),
    RateLimitTransform(max_calls=100, window_seconds=60),
    CachingTransform(ttl_seconds=300),
)

# Apply to multiple servers
gateway = FastMCP(name="Gateway")

gateway.mount(server_a, transforms=standard_pipeline)
gateway.mount(server_b, transforms=standard_pipeline)
gateway.mount(server_c, transforms=standard_pipeline)
```

## Custom Output Schemas

Transforms can modify tool output schemas for richer return types.

### Enhanced Output Schema

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import ToolTransform

class MetadataTransform(ToolTransform):
    """Add metadata to tool outputs."""

    async def execute(self, ctx: Context, **kwargs):
        """Wrap result with metadata."""
        import time

        start = time.time()
        result = await self.tool.execute(ctx, **kwargs)
        elapsed = time.time() - start

        return {
            "result": result,
            "metadata": {
                "tool": self.tool.name,
                "elapsed_ms": elapsed * 1000,
                "timestamp": time.time(),
                "client_id": ctx.client_id,
            }
        }

    def transform_schema(self, schema: dict) -> dict:
        """Update output schema to include metadata."""
        schema = schema.copy()
        schema["returns"] = {
            "type": "object",
            "properties": {
                "result": {"description": "Tool result"},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "elapsed_ms": {"type": "number"},
                        "timestamp": {"type": "number"},
                        "client_id": {"type": "string"},
                    }
                }
            }
        }
        return schema

source_server = FastMCP(name="Source")

@source_server.tool()
def calculate(x: float, y: float) -> float:
    return x + y

gateway = FastMCP(name="Gateway")
gateway.mount(source_server, transforms=[MetadataTransform()])

# Output becomes:
# {
#   "result": 15.0,
#   "metadata": {
#     "tool": "calculate",
#     "elapsed_ms": 0.123,
#     "timestamp": 1707523200.0,
#     "client_id": "client_123"
#   }
# }
```

## CLIO-Specific: Capability Filtering

CLIO uses transforms to dynamically filter capabilities based on agent tier and task requirements.

### Tier-Based Filtering

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import Enabled

class TierFilterTransform(Enabled):
    """Filter tools based on agent tier."""

    def __init__(self, allowed_tiers: list[int]):
        self.allowed_tiers = allowed_tiers

    def is_enabled(self, tool_name: str, ctx: Context) -> bool:
        """Check if tool is available for current tier."""
        # In CLIO, tier is passed via context or client_id
        current_tier = self._get_tier_from_context(ctx)
        return current_tier in self.allowed_tiers

    def _get_tier_from_context(self, ctx: Context) -> int:
        """Extract tier from context."""
        # Parse from ctx.client_id like "tier1_agent_id"
        if ctx.client_id and ctx.client_id.startswith("tier"):
            tier_str = ctx.client_id.split("_")[0].replace("tier", "")
            return int(tier_str)
        return 3  # Default to tier 3 (most restricted)

# Scientific tool server
scientific_server = FastMCP(name="Scientific Tools")

@scientific_server.tool()
def basic_analysis(data: list[float]) -> dict:
    """Basic analysis (available to all tiers)."""
    return {"mean": sum(data) / len(data)}

@scientific_server.tool()
def advanced_analysis(data: list[float]) -> dict:
    """Advanced analysis (Tier 1 and 2 only)."""
    import statistics
    return {
        "mean": statistics.mean(data),
        "stdev": statistics.stdev(data),
        "variance": statistics.variance(data)
    }

@scientific_server.tool()
def expert_analysis(data: list[float]) -> dict:
    """Expert analysis (Tier 1 only)."""
    import scipy.stats as stats
    return {
        "skewness": stats.skew(data),
        "kurtosis": stats.kurtosis(data)
    }

# Create gateways for different tiers
def create_tier_gateway(tier: int) -> FastMCP:
    gateway = FastMCP(name=f"Tier {tier} Gateway")

    if tier == 1:
        # Tier 1 (Main agent) - all tools
        gateway.mount(
            scientific_server,
            transforms=[TierFilterTransform(allowed_tiers=[1, 2, 3])]
        )
    elif tier == 2:
        # Tier 2 (Experts) - basic and advanced
        gateway.mount(
            scientific_server,
            transforms=[TierFilterTransform(allowed_tiers=[2, 3])]
        )
    else:
        # Tier 3 (Nanoagents) - basic only
        gateway.mount(
            scientific_server,
            transforms=[TierFilterTransform(allowed_tiers=[3])]
        )

    return gateway

# CLIO creates appropriate gateway for each agent
tier1_gateway = create_tier_gateway(1)  # Main agent
tier2_gateway = create_tier_gateway(2)  # DataExpert
tier3_gateway = create_tier_gateway(3)  # Nanoagents
```

### Task-Specific Tool Selection

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import Enabled

class TaskFilterTransform(Enabled):
    """Enable tools based on task requirements."""

    def __init__(self, task_type: str):
        self.task_type = task_type
        # Map task types to required tools
        self.task_tools = {
            "hdf5_analysis": ["read_hdf5", "analyze_hdf5"],
            "parquet_analysis": ["read_parquet", "analyze_parquet"],
            "visualization": ["plot_data", "create_chart"],
        }

    def is_enabled(self, tool_name: str, ctx: Context) -> bool:
        """Check if tool is needed for current task."""
        required_tools = self.task_tools.get(self.task_type, [])
        return not required_tools or tool_name in required_tools

# All scientific tools
all_tools_server = FastMCP(name="All Tools")

@all_tools_server.tool()
def read_hdf5(path: str) -> dict:
    """Read HDF5 file."""
    return {}

@all_tools_server.tool()
def analyze_hdf5(data: dict) -> dict:
    """Analyze HDF5 data."""
    return {}

@all_tools_server.tool()
def read_parquet(path: str) -> dict:
    """Read Parquet file."""
    return {}

@all_tools_server.tool()
def plot_data(data: dict) -> str:
    """Plot data."""
    return "plot.png"

# CLIO creates task-specific gateway
def create_task_gateway(task_type: str) -> FastMCP:
    gateway = FastMCP(name=f"Task Gateway: {task_type}")
    gateway.mount(
        all_tools_server,
        transforms=[TaskFilterTransform(task_type=task_type)]
    )
    return gateway

# For HDF5 task, only HDF5 tools available
hdf5_gateway = create_task_gateway("hdf5_analysis")
```

### ARC-Integrated Transform

```python
from fastmcp import FastMCP, Context
from fastmcp.transforms import ToolTransform

class ARCCachingTransform(ToolTransform):
    """Integrate with CLIO's ARC memory system."""

    def __init__(self, arc_memory):
        self.arc_memory = arc_memory

    async def execute(self, ctx: Context, **kwargs):
        """Check ARC cache before execution."""
        # Generate cache key
        cache_key = self._generate_key(self.tool.name, kwargs)

        # Check ARC cache
        cached_result = self.arc_memory.get_cached_tool_result(
            self.tool.name,
            kwargs
        )

        if cached_result is not None:
            await ctx.info(f"ARC cache hit for {self.tool.name}")
            # Store cache hit in ARC metrics
            self.arc_memory.record_cache_hit(self.tool.name)
            return cached_result

        # Execute tool
        result = await self.tool.execute(ctx, **kwargs)

        # Store in ARC
        self.arc_memory.cache_tool_result(
            self.tool.name,
            kwargs,
            result
        )

        return result

    def _generate_key(self, tool_name: str, kwargs: dict) -> str:
        """Generate ARC-compatible cache key."""
        import hashlib
        import json
        key_data = {"tool": tool_name, "params": kwargs}
        return hashlib.sha256(
            json.dumps(key_data, sort_keys=True).encode()
        ).hexdigest()

# CLIO integration
from clio_agent.arc import ARCMemory

arc_memory = ARCMemory()

scientific_server = FastMCP(name="Scientific")

@scientific_server.tool()
def compute_intensive_operation(data: list[float]) -> dict:
    """Expensive computation."""
    return {"result": sum(data)}

gateway = FastMCP(name="CLIO Gateway")
gateway.mount(
    scientific_server,
    transforms=[ARCCachingTransform(arc_memory=arc_memory)]
)

# All tool calls automatically cached in ARC
```

## Best Practices

1. **Compose Transforms**: Chain simple transforms rather than creating complex monolithic ones.

2. **Schema Consistency**: Ensure transformed schemas accurately reflect actual behavior.

3. **Performance**: Keep transforms lightweight; they execute on every tool call.

4. **Error Handling**: Transform errors should be clear and actionable.

5. **Documentation**: Document transform behavior for tool users.

6. **Testing**: Test transforms independently before chaining.

7. **State Management**: Be careful with stateful transforms (caching, rate limiting) in distributed environments.

## Summary

FastMCP transforms enable:
- Dynamic capability modification without changing source servers
- Layered behavior (logging, auth, caching) via composition
- Context-aware tool filtering for security and efficiency
- Custom output schemas for richer return types
- Integration with external systems (like CLIO's ARC)

CLIO leverages transforms to implement its 3-tier architecture, ensuring each agent tier has appropriate tool access and that all operations integrate with the ARC memory system.
