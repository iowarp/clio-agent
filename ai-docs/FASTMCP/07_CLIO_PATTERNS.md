# FastMCP Patterns for CLIO Agent
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

CLIO-specific FastMCP patterns for building scientific tool servers, gateway orchestration, and DSPy integration.

---

## Table of Contents

1. [CLIO Gateway Pattern](#clio-gateway-pattern)
2. [Scientific Tool Server Architecture](#scientific-tool-server-architecture)
3. [Tool Registration with Capability Metadata](#tool-registration-with-capability-metadata)
4. [Dynamic Tool Discovery and Routing](#dynamic-tool-discovery-and-routing)
5. [Context Threading Through Agent Tiers](#context-threading-through-agent-tiers)
6. [FastMCP + DSPy Integration](#fastmcp--dspy-integration)
7. [Resource-Based Configuration](#resource-based-configuration)
8. [Progress Reporting for Long-Running Operations](#progress-reporting-for-long-running-operations)
9. [Error Handling with Graceful Degradation](#error-handling-with-graceful-degradation)
10. [Complete Examples](#complete-examples)

---

## CLIO Gateway Pattern

**Problem**: CLIO Agent needs to aggregate 15+ scientific tool servers (HDF5, ADIOS, Parquet, SLURM, Darshan, etc.) into a unified interface for expert agents.

**Solution**: Gateway server pattern using `mount()` to compose multiple specialized servers.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│               CLIO Gateway Server                       │
│  (Main Agent / Expert Agents interact here)             │
├─────────────────────────────────────────────────────────┤
│  - Tool routing based on file format detection          │
│  - Capability-based server selection                    │
│  - Context propagation to backend servers               │
│  - ARC memory integration for caching                   │
│  - Error aggregation and graceful degradation           │
└────────────┬────────────────────────────────────────────┘
             │
    ┌────────┴────────┬────────────┬──────────────┐
    ▼                 ▼            ▼              ▼
┌─────────┐    ┌──────────┐  ┌─────────┐  ┌──────────┐
│ HDF5    │    │ Parquet  │  │ SLURM   │  │ NetCDF   │
│ Server  │    │ Server   │  │ Server  │  │ Server   │
└─────────┘    └──────────┘  └─────────┘  └──────────┘
```

### Implementation

```python
from fastmcp import FastMCP
from typing import Optional
import mimetypes
from pathlib import Path

class CLIOGateway:
    """CLIO Agent MCP Gateway orchestrating scientific tool servers."""

    def __init__(self):
        self.gateway = FastMCP("clio-gateway")
        self.backend_servers = {}

        # Initialize and mount backend servers
        self._setup_backend_servers()

        # Add gateway-level coordination tools
        self._register_gateway_tools()

    def _setup_backend_servers(self):
        """Initialize and mount all backend servers."""
        # Create backend servers
        from .servers.hdf5_server import create_hdf5_server
        from .servers.parquet_server import create_parquet_server
        from .servers.slurm_server import create_slurm_server

        hdf5_server = create_hdf5_server()
        parquet_server = create_parquet_server()
        slurm_server = create_slurm_server()

        # Mount servers with namespacing
        self.gateway.mount("/hdf5", hdf5_server)
        self.gateway.mount("/parquet", parquet_server)
        self.gateway.mount("/slurm", slurm_server)

        # Store for routing logic
        self.backend_servers = {
            "hdf5": hdf5_server,
            "parquet": parquet_server,
            "slurm": slurm_server,
        }

    def _register_gateway_tools(self):
        """Register gateway-level coordination tools."""

        @self.gateway.tool()
        def analyze_file(
            filepath: str,
            format: Optional[str] = None
        ) -> dict:
            """
            Analyze scientific data file by auto-detecting format.

            Args:
                filepath: Path to data file
                format: Optional format override (hdf5, parquet, netcdf)

            Returns:
                Analysis results including format, size, metadata
            """
            # Auto-detect format if not provided
            if format is None:
                format = self._detect_format(filepath)

            if format not in self.backend_servers:
                return {
                    "error": f"Unsupported format: {format}",
                    "supported_formats": list(self.backend_servers.keys())
                }

            # Route to appropriate backend
            return {
                "filepath": filepath,
                "format": format,
                "backend": f"{format}_server",
                "status": "ready for analysis"
            }

        @self.gateway.tool()
        def list_capabilities() -> dict:
            """List all available tool capabilities across servers."""
            capabilities = {}
            for name, server in self.backend_servers.items():
                # Get tools from backend server
                tools = server.list_tools()
                capabilities[name] = {
                    "server": name,
                    "tool_count": len(tools),
                    "tools": [t.name for t in tools]
                }
            return capabilities

    def _detect_format(self, filepath: str) -> str:
        """Detect file format from extension."""
        path = Path(filepath)
        ext = path.suffix.lower()

        format_map = {
            ".h5": "hdf5",
            ".hdf5": "hdf5",
            ".parquet": "parquet",
            ".pq": "parquet",
            ".nc": "netcdf",
            ".nc4": "netcdf",
        }

        return format_map.get(ext, "unknown")

    def get_server(self) -> FastMCP:
        """Get the FastMCP gateway server instance."""
        return self.gateway


# Usage in CLIO Agent
gateway = CLIOGateway()
mcp_server = gateway.get_server()
```

---

## Scientific Tool Server Architecture

**Pattern**: Each scientific data format gets a dedicated MCP server with format-specific tools.

### HDF5 Server Example

```python
from fastmcp import FastMCP
import h5py
import numpy as np
from pathlib import Path
from typing import Optional

def create_hdf5_server() -> FastMCP:
    """Create HDF5 analysis server."""
    mcp = FastMCP("hdf5-analysis")

    @mcp.tool()
    def list_datasets(filepath: str) -> list[str]:
        """
        List all datasets in HDF5 file.

        Args:
            filepath: Path to HDF5 file

        Returns:
            List of dataset paths
        """
        datasets = []
        with h5py.File(filepath, "r") as f:
            def visit_func(name, obj):
                if isinstance(obj, h5py.Dataset):
                    datasets.append(name)
            f.visititems(visit_func)
        return datasets

    @mcp.tool()
    def analyze_dataset(filepath: str, dataset_name: str) -> dict:
        """
        Analyze HDF5 dataset statistics.

        Args:
            filepath: Path to HDF5 file
            dataset_name: Name of dataset to analyze

        Returns:
            Statistics dictionary with mean, std, min, max, shape
        """
        with h5py.File(filepath, "r") as f:
            if dataset_name not in f:
                raise ValueError(f"Dataset not found: {dataset_name}")

            dataset = f[dataset_name]
            data = dataset[:]

            # Basic statistics
            stats = {
                "shape": list(data.shape),
                "dtype": str(data.dtype),
                "size_bytes": data.nbytes,
                "compression": dataset.compression or "none",
            }

            # Numerical statistics (if numeric data)
            if np.issubdtype(data.dtype, np.number):
                stats.update({
                    "mean": float(np.mean(data)),
                    "std": float(np.std(data)),
                    "min": float(np.min(data)),
                    "max": float(np.max(data)),
                })

            return stats

    @mcp.tool()
    def optimize_chunking(
        filepath: str,
        dataset_name: str,
        access_pattern: str = "sequential"
    ) -> dict:
        """
        Recommend optimal chunking strategy for HDF5 dataset.

        Args:
            filepath: Path to HDF5 file
            dataset_name: Dataset to analyze
            access_pattern: Access pattern (sequential, random, strided)

        Returns:
            Recommendations for chunk size and compression
        """
        with h5py.File(filepath, "r") as f:
            dataset = f[dataset_name]
            current_chunks = dataset.chunks

            # Analyze current configuration
            shape = dataset.shape
            dtype = dataset.dtype

            # Calculate optimal chunks based on access pattern
            if access_pattern == "sequential":
                # Optimize for sequential reads (larger chunks along first dim)
                suggested_chunks = (min(shape[0], 1000),) + shape[1:]
            elif access_pattern == "random":
                # Optimize for random access (smaller, balanced chunks)
                suggested_chunks = tuple(min(s, 100) for s in shape)
            else:  # strided
                suggested_chunks = tuple(min(s, 500) for s in shape)

            return {
                "current_chunks": current_chunks,
                "suggested_chunks": suggested_chunks,
                "current_compression": dataset.compression,
                "suggested_compression": "gzip",
                "compression_level": 6,
                "rationale": f"Optimized for {access_pattern} access pattern"
            }

    @mcp.resource("hdf5://{filepath}/metadata")
    def get_file_metadata(filepath: str) -> str:
        """Get HDF5 file metadata as formatted string."""
        with h5py.File(filepath, "r") as f:
            lines = [f"HDF5 File: {filepath}"]
            lines.append(f"Attributes: {dict(f.attrs)}")
            lines.append(f"Datasets: {len(list(f.keys()))}")
            return "\n".join(lines)

    return mcp
```

### Parquet Server Example

```python
from fastmcp import FastMCP
import pyarrow.parquet as pq
import pyarrow.compute as pc

def create_parquet_server() -> FastMCP:
    """Create Parquet analysis server."""
    mcp = FastMCP("parquet-analysis")

    @mcp.tool()
    def analyze_schema(filepath: str) -> dict:
        """
        Analyze Parquet file schema.

        Args:
            filepath: Path to Parquet file

        Returns:
            Schema information including columns, types, metadata
        """
        table = pq.read_table(filepath)
        schema = table.schema

        columns = []
        for i in range(len(schema)):
            field = schema.field(i)
            columns.append({
                "name": field.name,
                "type": str(field.type),
                "nullable": field.nullable,
            })

        return {
            "num_columns": len(columns),
            "num_rows": table.num_rows,
            "columns": columns,
            "metadata": schema.metadata or {},
        }

    @mcp.tool()
    def query_data(
        filepath: str,
        columns: Optional[list[str]] = None,
        limit: int = 100
    ) -> dict:
        """
        Query Parquet file data.

        Args:
            filepath: Path to Parquet file
            columns: Columns to select (None = all)
            limit: Max rows to return

        Returns:
            Query results as dictionary
        """
        table = pq.read_table(filepath, columns=columns)

        # Limit rows
        if limit < table.num_rows:
            table = table.slice(0, limit)

        # Convert to dictionary
        return {
            "num_rows": table.num_rows,
            "columns": table.column_names,
            "data": table.to_pydict()
        }

    @mcp.tool()
    def compute_statistics(filepath: str, column: str) -> dict:
        """
        Compute statistics for a column.

        Args:
            filepath: Path to Parquet file
            column: Column name to analyze

        Returns:
            Statistics dictionary
        """
        table = pq.read_table(filepath)
        col = table.column(column)

        stats = {
            "count": len(col),
            "null_count": pc.sum(pc.is_null(col)).as_py(),
        }

        # Numeric statistics
        if pc.is_integer(col.type) or pc.is_floating(col.type):
            stats.update({
                "min": pc.min(col).as_py(),
                "max": pc.max(col).as_py(),
                "mean": pc.mean(col).as_py(),
            })

        return stats

    return mcp
```

---

## Tool Registration with Capability Metadata

**Pattern**: Register tools with capability metadata for intelligent routing by CLIO's Agent Registry.

```python
from fastmcp import FastMCP
from dataclasses import dataclass
from typing import List

@dataclass
class ToolCapability:
    """Capability metadata for a tool."""
    formats: List[str]  # Supported file formats
    operations: List[str]  # Operations (read, write, analyze, optimize)
    requires_compute: bool  # Requires HPC resources
    estimated_cost: str  # Cost estimate (low, medium, high)


def create_capability_aware_server() -> FastMCP:
    """Create server with capability metadata."""
    mcp = FastMCP("capability-server")

    # Store capabilities
    mcp._capabilities = {}

    def register_tool_with_capabilities(capability: ToolCapability):
        """Decorator to register tool with capabilities."""
        def decorator(func):
            tool_decorator = mcp.tool()
            tool_func = tool_decorator(func)

            # Store capability metadata
            mcp._capabilities[func.__name__] = capability

            return tool_func
        return decorator

    @register_tool_with_capabilities(ToolCapability(
        formats=["hdf5"],
        operations=["read", "analyze"],
        requires_compute=False,
        estimated_cost="low"
    ))
    def quick_analysis(filepath: str) -> dict:
        """Fast analysis without heavy computation."""
        return {"status": "quick analysis"}

    @register_tool_with_capabilities(ToolCapability(
        formats=["hdf5", "netcdf"],
        operations=["optimize"],
        requires_compute=True,
        estimated_cost="high"
    ))
    def deep_optimization(filepath: str) -> dict:
        """Intensive optimization requiring HPC resources."""
        return {"status": "deep optimization"}

    @mcp.tool()
    def query_capabilities(operation: str, format: str) -> list[str]:
        """
        Query tools by capabilities.

        Args:
            operation: Required operation (read, write, analyze, optimize)
            format: File format (hdf5, parquet, etc.)

        Returns:
            List of tool names matching criteria
        """
        matching_tools = []
        for tool_name, capability in mcp._capabilities.items():
            if operation in capability.operations and format in capability.formats:
                matching_tools.append({
                    "tool": tool_name,
                    "requires_compute": capability.requires_compute,
                    "cost": capability.estimated_cost
                })
        return matching_tools

    return mcp
```

---

## Dynamic Tool Discovery and Routing

**Pattern**: CLIO Main Agent dynamically discovers and routes to tools based on query intent.

```python
from fastmcp import FastMCP, Client
from typing import Optional
import asyncio

class DynamicToolRouter:
    """Router for dynamic tool discovery and execution."""

    def __init__(self, gateway: FastMCP):
        self.gateway = gateway
        self.tool_cache = {}

    async def discover_tools(self) -> list[dict]:
        """Discover all available tools from gateway."""
        async with Client(self.gateway) as client:
            tools = await client.list_tools()

            tool_info = []
            for tool in tools:
                tool_info.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema.get("properties", {})
                })

            return tool_info

    async def route_query(
        self,
        query: str,
        context: Optional[dict] = None
    ) -> dict:
        """
        Route query to appropriate tool.

        Args:
            query: User query
            context: Optional context (file path, format, etc.)

        Returns:
            Routing decision with tool name and parameters
        """
        # Extract intent (simplified - in CLIO, uses DSPy signature)
        intent = self._extract_intent(query, context)

        # Find matching tool
        tools = await self.discover_tools()
        selected_tool = self._match_tool(intent, tools)

        return {
            "query": query,
            "intent": intent,
            "selected_tool": selected_tool["name"],
            "parameters": self._extract_parameters(query, selected_tool),
        }

    def _extract_intent(self, query: str, context: Optional[dict]) -> dict:
        """Extract intent from query."""
        # Simplified intent extraction
        intent = {"operation": "unknown"}

        query_lower = query.lower()
        if "analyze" in query_lower or "statistics" in query_lower:
            intent["operation"] = "analyze"
        elif "optimize" in query_lower:
            intent["operation"] = "optimize"
        elif "list" in query_lower:
            intent["operation"] = "list"

        # Add format from context
        if context and "filepath" in context:
            filepath = context["filepath"]
            if filepath.endswith(".h5") or filepath.endswith(".hdf5"):
                intent["format"] = "hdf5"
            elif filepath.endswith(".parquet"):
                intent["format"] = "parquet"

        return intent

    def _match_tool(self, intent: dict, tools: list[dict]) -> dict:
        """Match intent to tool."""
        operation = intent.get("operation", "")

        # Simple keyword matching (in CLIO, uses semantic similarity)
        for tool in tools:
            desc_lower = tool["description"].lower()
            if operation in desc_lower:
                return tool

        # Default to first tool
        return tools[0] if tools else {}

    def _extract_parameters(self, query: str, tool: dict) -> dict:
        """Extract parameters for tool."""
        # Simplified parameter extraction
        params = {}

        # Extract filepath if present
        words = query.split()
        for word in words:
            if word.endswith((".h5", ".hdf5", ".parquet")):
                params["filepath"] = word

        return params


# Usage in CLIO Main Agent
async def main_agent_workflow():
    """Example CLIO Main Agent workflow."""
    gateway = CLIOGateway().get_server()
    router = DynamicToolRouter(gateway)

    # User query
    query = "Analyze the dataset in /data/experiment.h5"
    context = {"filepath": "/data/experiment.h5"}

    # Route query
    routing = await router.route_query(query, context)
    print(f"Routing: {routing}")

    # Execute tool
    async with Client(gateway) as client:
        result = await client.call_tool(
            routing["selected_tool"],
            routing["parameters"]
        )
        print(f"Result: {result}")
```

---

## Context Threading Through Agent Tiers

**Pattern**: Thread conversation context through CLIO's 3-tier hierarchy (Main → Expert → Nanoagent).

```python
from fastmcp import FastMCP
from dataclasses import dataclass
from typing import Optional, Any
from datetime import datetime

@dataclass
class AgentContext:
    """Context passed through agent tiers."""
    session_id: str
    user_query: str
    tier: int  # 1=Main, 2=Expert, 3=Nanoagent
    parent_agent: Optional[str]
    metadata: dict[str, Any]
    timestamp: datetime


def create_context_aware_server(tier: int, agent_name: str) -> FastMCP:
    """Create server that propagates context."""
    mcp = FastMCP(f"{agent_name}-tier{tier}")

    @mcp.tool()
    def process_with_context(
        query: str,
        context: dict
    ) -> dict:
        """
        Process query with inherited context.

        Args:
            query: Task query
            context: Context from parent agent

        Returns:
            Result with enriched context
        """
        # Parse context
        agent_context = AgentContext(
            session_id=context.get("session_id", "unknown"),
            user_query=context.get("user_query", query),
            tier=tier,
            parent_agent=context.get("agent_name"),
            metadata=context.get("metadata", {}),
            timestamp=datetime.now()
        )

        # Process query (simplified)
        result = {
            "agent": agent_name,
            "tier": tier,
            "processed_query": query,
            "parent": agent_context.parent_agent,
            "session_id": agent_context.session_id,
        }

        # Add to metadata for child agents
        result["context"] = {
            "session_id": agent_context.session_id,
            "user_query": agent_context.user_query,
            "agent_name": agent_name,
            "metadata": {
                **agent_context.metadata,
                f"tier{tier}_agent": agent_name,
                f"tier{tier}_timestamp": agent_context.timestamp.isoformat(),
            }
        }

        return result

    return mcp


# Example 3-tier workflow
async def three_tier_workflow():
    """Demonstrate context threading through 3 tiers."""
    # Create tier servers
    main_agent = create_context_aware_server(1, "MainAgent")
    data_expert = create_context_aware_server(2, "DataExpert")
    hdf5_nanoagent = create_context_aware_server(3, "HDF5Nanoagent")

    # Initial context from user
    initial_context = {
        "session_id": "session_123",
        "user_query": "Analyze HDF5 file",
        "agent_name": "User",
        "metadata": {"source": "cli"}
    }

    # Tier 1: Main Agent
    async with Client(main_agent) as client:
        tier1_result = await client.call_tool(
            "process_with_context",
            {"query": "Route to data expert", "context": initial_context}
        )

    # Tier 2: Data Expert (inherits Tier 1 context)
    async with Client(data_expert) as client:
        tier2_result = await client.call_tool(
            "process_with_context",
            {"query": "Spawn HDF5 nanoagent", "context": tier1_result["context"]}
        )

    # Tier 3: Nanoagent (inherits Tier 1+2 context)
    async with Client(hdf5_nanoagent) as client:
        tier3_result = await client.call_tool(
            "process_with_context",
            {"query": "Analyze specific dataset", "context": tier2_result["context"]}
        )

    # Context is threaded through all tiers
    print(f"Tier 3 knows about: {tier3_result['context']['metadata']}")
    # Output includes: tier1_agent, tier2_agent, tier3_agent, timestamps, etc.
```

---

## FastMCP + DSPy Integration

**Pattern**: Bridge FastMCP tools into DSPy for use in CLIO agent reasoning.

```python
import dspy
from fastmcp import FastMCP, Client
import asyncio
from typing import Any

class FastMCPTool(dspy.Tool):
    """DSPy tool wrapper for FastMCP tools."""

    def __init__(self, mcp_server: FastMCP, tool_name: str):
        self.mcp_server = mcp_server
        self.tool_name = tool_name

        # Get tool metadata
        self.tool_metadata = self._get_tool_metadata()

        # Initialize DSPy Tool
        super().__init__(
            name=tool_name,
            description=self.tool_metadata.get("description", ""),
            input_schema=self.tool_metadata.get("inputSchema", {})
        )

    def _get_tool_metadata(self) -> dict:
        """Get tool metadata from MCP server."""
        async def fetch():
            async with Client(self.mcp_server) as client:
                tools = await client.list_tools()
                for tool in tools:
                    if tool.name == self.tool_name:
                        return {
                            "description": tool.description,
                            "inputSchema": tool.inputSchema
                        }
                return {}

        return asyncio.run(fetch())

    def __call__(self, **kwargs) -> Any:
        """Execute MCP tool from DSPy."""
        async def execute():
            async with Client(self.mcp_server) as client:
                result = await client.call_tool(self.tool_name, kwargs)
                return result

        return asyncio.run(execute())


# Usage in CLIO Agent
class CLIODataExpert(dspy.Module):
    """CLIO Data Expert using FastMCP tools."""

    def __init__(self, mcp_gateway: FastMCP):
        super().__init__()

        # Bridge MCP tools to DSPy
        self.analyze_hdf5 = FastMCPTool(mcp_gateway, "analyze_dataset")
        self.list_datasets = FastMCPTool(mcp_gateway, "list_datasets")

        # DSPy reasoning module
        self.reason = dspy.ChainOfThought(
            "query, available_tools -> reasoning, tool_name, tool_params"
        )

    def forward(self, query: str, filepath: str):
        """Process query using MCP tools."""
        # Reason about which tool to use
        reasoning_result = self.reason(
            query=query,
            available_tools="analyze_dataset, list_datasets"
        )

        tool_name = reasoning_result.tool_name

        # Execute selected tool
        if tool_name == "analyze_dataset":
            # Parse params from reasoning
            dataset_name = reasoning_result.tool_params.get("dataset_name", "data")
            result = self.analyze_hdf5(filepath=filepath, dataset_name=dataset_name)
        elif tool_name == "list_datasets":
            result = self.list_datasets(filepath=filepath)
        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        return {
            "reasoning": reasoning_result.reasoning,
            "tool_used": tool_name,
            "result": result
        }


# Example usage
def main():
    gateway = CLIOGateway().get_server()

    # Create CLIO expert with MCP tools
    expert = CLIODataExpert(gateway)

    # Process query
    result = expert.forward(
        query="What datasets are in the file?",
        filepath="/data/experiment.h5"
    )

    print(f"Reasoning: {result['reasoning']}")
    print(f"Tool: {result['tool_used']}")
    print(f"Result: {result['result']}")
```

---

## Resource-Based Configuration

**Pattern**: Use MCP resources for dynamic configuration of CLIO servers.

```python
from fastmcp import FastMCP
import json

def create_configurable_server() -> FastMCP:
    """Server with resource-based configuration."""
    mcp = FastMCP("configurable-server")

    # Configuration state
    config = {
        "cache_enabled": True,
        "max_file_size_mb": 1000,
        "supported_formats": ["hdf5", "parquet", "netcdf"],
        "parallel_workers": 4,
    }

    @mcp.resource("config://server/settings")
    def get_server_config() -> str:
        """Get server configuration."""
        return json.dumps(config, indent=2)

    @mcp.resource("config://server/capabilities")
    def get_capabilities() -> str:
        """Get server capabilities."""
        capabilities = {
            "formats": config["supported_formats"],
            "max_file_size_mb": config["max_file_size_mb"],
            "features": ["caching", "parallel_processing"]
        }
        return json.dumps(capabilities, indent=2)

    @mcp.tool()
    def update_config(key: str, value: Any) -> dict:
        """
        Update server configuration.

        Args:
            key: Configuration key
            value: New value

        Returns:
            Updated configuration
        """
        if key not in config:
            raise ValueError(f"Unknown config key: {key}")

        config[key] = value
        return {"updated": key, "value": value, "config": config}

    @mcp.tool()
    def process_file(filepath: str) -> dict:
        """Process file using current configuration."""
        # Check configuration
        if not config["cache_enabled"]:
            return {"status": "cache disabled, processing without cache"}

        # Use configuration settings
        return {
            "filepath": filepath,
            "cache_enabled": config["cache_enabled"],
            "workers": config["parallel_workers"]
        }

    return mcp


# Usage: Clients can read configuration via resources
async def configure_and_use_server():
    server = create_configurable_server()

    async with Client(server) as client:
        # Read configuration
        config_str = await client.read_resource("config://server/settings")
        config = json.loads(config_str)
        print(f"Current config: {config}")

        # Update configuration
        await client.call_tool("update_config", {
            "key": "parallel_workers",
            "value": 8
        })

        # Process with new configuration
        result = await client.call_tool("process_file", {
            "filepath": "/data/large_file.h5"
        })
        print(f"Processing with {result['workers']} workers")
```

---

## Progress Reporting for Long-Running Operations

**Pattern**: Report progress for scientific analyses that take seconds/minutes.

```python
from fastmcp import FastMCP
import asyncio
from typing import AsyncIterator

def create_progress_aware_server() -> FastMCP:
    """Server with progress reporting."""
    mcp = FastMCP("progress-server")

    @mcp.tool()
    async def analyze_large_dataset(
        filepath: str,
        progress_callback: Optional[callable] = None
    ) -> dict:
        """
        Analyze large dataset with progress reporting.

        Args:
            filepath: Path to dataset
            progress_callback: Optional callback for progress updates

        Returns:
            Analysis results
        """
        total_steps = 5
        results = {}

        # Step 1: Load data
        if progress_callback:
            await progress_callback({"step": 1, "total": total_steps, "status": "Loading data"})
        await asyncio.sleep(0.5)  # Simulate work
        results["data_loaded"] = True

        # Step 2: Compute statistics
        if progress_callback:
            await progress_callback({"step": 2, "total": total_steps, "status": "Computing statistics"})
        await asyncio.sleep(0.5)
        results["mean"] = 42.0

        # Step 3: Analyze distributions
        if progress_callback:
            await progress_callback({"step": 3, "total": total_steps, "status": "Analyzing distributions"})
        await asyncio.sleep(0.5)
        results["distribution"] = "normal"

        # Step 4: Detect anomalies
        if progress_callback:
            await progress_callback({"step": 4, "total": total_steps, "status": "Detecting anomalies"})
        await asyncio.sleep(0.5)
        results["anomalies"] = []

        # Step 5: Generate report
        if progress_callback:
            await progress_callback({"step": 5, "total": total_steps, "status": "Generating report"})
        await asyncio.sleep(0.5)
        results["complete"] = True

        return results

    return mcp


# Usage with progress tracking
async def analyze_with_progress():
    server = create_progress_aware_server()

    progress_updates = []

    async def track_progress(update: dict):
        """Callback to track progress."""
        progress_updates.append(update)
        percent = (update["step"] / update["total"]) * 100
        print(f"[{percent:.0f}%] {update['status']}")

    async with Client(server) as client:
        result = await client.call_tool(
            "analyze_large_dataset",
            {
                "filepath": "/data/large_dataset.h5",
                "progress_callback": track_progress
            }
        )

    print(f"Analysis complete: {result}")
    print(f"Total progress updates: {len(progress_updates)}")
```

---

## Error Handling with Graceful Degradation

**Pattern**: Handle errors gracefully and fall back to alternative approaches.

```python
from fastmcp import FastMCP
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def create_robust_server() -> FastMCP:
    """Server with graceful error handling."""
    mcp = FastMCP("robust-server")

    @mcp.tool()
    def analyze_with_fallback(
        filepath: str,
        method: str = "optimal"
    ) -> dict:
        """
        Analyze file with fallback to simpler methods on failure.

        Args:
            filepath: Path to file
            method: Analysis method (optimal, fast, basic)

        Returns:
            Analysis results with method used
        """
        methods = ["optimal", "fast", "basic"]

        # Try methods in order until one succeeds
        for attempt_method in methods[methods.index(method):]:
            try:
                result = _analyze_with_method(filepath, attempt_method)
                return {
                    "success": True,
                    "method_requested": method,
                    "method_used": attempt_method,
                    "result": result
                }
            except Exception as e:
                logger.warning(f"Method {attempt_method} failed: {e}")
                if attempt_method == "basic":
                    # Last resort failed
                    return {
                        "success": False,
                        "method_requested": method,
                        "error": str(e),
                        "message": "All analysis methods failed"
                    }
                # Continue to next method

    def _analyze_with_method(filepath: str, method: str) -> dict:
        """Internal analysis implementation."""
        if method == "optimal":
            # Complex analysis that might fail
            if "corrupt" in filepath:
                raise ValueError("File appears corrupt")
            return {"method": "optimal", "quality": "high"}
        elif method == "fast":
            # Simpler analysis
            if "missing" in filepath:
                raise FileNotFoundError("File not found")
            return {"method": "fast", "quality": "medium"}
        else:  # basic
            # Most basic analysis (always succeeds)
            return {"method": "basic", "quality": "low"}

    @mcp.tool()
    def multi_file_analysis(filepaths: list[str]) -> dict:
        """
        Analyze multiple files, continue on failures.

        Args:
            filepaths: List of file paths

        Returns:
            Results for all files (successful and failed)
        """
        results = {
            "successful": [],
            "failed": [],
            "summary": {}
        }

        for filepath in filepaths:
            try:
                analysis = _analyze_with_method(filepath, "fast")
                results["successful"].append({
                    "filepath": filepath,
                    "analysis": analysis
                })
            except Exception as e:
                results["failed"].append({
                    "filepath": filepath,
                    "error": str(e)
                })

        results["summary"] = {
            "total": len(filepaths),
            "successful": len(results["successful"]),
            "failed": len(results["failed"]),
            "success_rate": len(results["successful"]) / len(filepaths)
        }

        return results

    return mcp


# Usage
async def robust_analysis():
    server = create_robust_server()

    async with Client(server) as client:
        # Single file with fallback
        result = await client.call_tool("analyze_with_fallback", {
            "filepath": "/data/corrupt_file.h5",
            "method": "optimal"
        })
        print(f"Fell back to: {result['method_used']}")

        # Multiple files (some may fail)
        batch_result = await client.call_tool("multi_file_analysis", {
            "filepaths": [
                "/data/file1.h5",
                "/data/missing_file.h5",
                "/data/file2.h5"
            ]
        })
        print(f"Success rate: {batch_result['summary']['success_rate']:.0%}")
```

---

## Complete Examples

### Complete Scientific Analysis Server

```python
from fastmcp import FastMCP
import h5py
import numpy as np
from pathlib import Path
from typing import Optional, List
import json

def create_complete_scientific_server() -> FastMCP:
    """Complete scientific analysis server for CLIO."""
    mcp = FastMCP("scientific-analysis")

    # ---------- HDF5 Tools ----------

    @mcp.tool()
    def hdf5_list_datasets(filepath: str) -> List[str]:
        """List all datasets in HDF5 file."""
        datasets = []
        with h5py.File(filepath, "r") as f:
            f.visit(lambda name: datasets.append(name) if isinstance(f[name], h5py.Dataset) else None)
        return datasets

    @mcp.tool()
    def hdf5_analyze_dataset(filepath: str, dataset: str) -> dict:
        """Analyze HDF5 dataset statistics."""
        with h5py.File(filepath, "r") as f:
            data = f[dataset][:]
            return {
                "shape": list(data.shape),
                "dtype": str(data.dtype),
                "mean": float(np.mean(data)),
                "std": float(np.std(data)),
                "min": float(np.min(data)),
                "max": float(np.max(data)),
            }

    @mcp.tool()
    def hdf5_optimize_chunking(filepath: str, dataset: str) -> dict:
        """Recommend optimal chunking for dataset."""
        with h5py.File(filepath, "r") as f:
            ds = f[dataset]
            shape = ds.shape
            suggested = tuple(min(s, 1000) for s in shape)
            return {
                "current_chunks": ds.chunks,
                "suggested_chunks": suggested,
                "compression": ds.compression,
                "suggested_compression": "gzip"
            }

    # ---------- Resources ----------

    @mcp.resource("analysis://{filepath}/summary")
    def file_summary(filepath: str) -> str:
        """Get file analysis summary."""
        with h5py.File(filepath, "r") as f:
            datasets = list(f.keys())
            attrs = dict(f.attrs)

        summary = {
            "filepath": filepath,
            "num_datasets": len(datasets),
            "datasets": datasets,
            "attributes": attrs
        }
        return json.dumps(summary, indent=2)

    # ---------- Configuration ----------

    @mcp.resource("config://settings")
    def get_config() -> str:
        """Server configuration."""
        config = {
            "cache_enabled": True,
            "max_file_size_gb": 10,
            "supported_formats": ["hdf5", "h5"]
        }
        return json.dumps(config, indent=2)

    return mcp
```

### Complete CLIO Gateway Server

```python
from fastmcp import FastMCP
from pathlib import Path
from typing import Optional, Dict, Any

class CompleteCLIOGateway:
    """Complete CLIO gateway implementation."""

    def __init__(self):
        self.gateway = FastMCP("clio-production-gateway")
        self.backends = {}
        self._setup_backends()
        self._register_gateway_tools()

    def _setup_backends(self):
        """Setup all backend servers."""
        # Import backend creators
        self.backends["hdf5"] = create_complete_scientific_server()
        # In production: add more backends
        # self.backends["parquet"] = create_parquet_server()
        # self.backends["slurm"] = create_slurm_server()

        # Mount backends
        for name, server in self.backends.items():
            self.gateway.mount(f"/{name}", server)

    def _register_gateway_tools(self):
        """Register gateway orchestration tools."""

        @self.gateway.tool()
        def analyze_file_auto(filepath: str) -> dict:
            """
            Automatically analyze file by detecting format.

            Args:
                filepath: Path to scientific data file

            Returns:
                Analysis results with format detection
            """
            # Detect format
            ext = Path(filepath).suffix.lower()
            format_map = {
                ".h5": "hdf5",
                ".hdf5": "hdf5",
                ".parquet": "parquet",
                ".pq": "parquet",
            }

            detected_format = format_map.get(ext, "unknown")

            if detected_format not in self.backends:
                return {
                    "error": f"Unsupported format: {detected_format}",
                    "supported": list(self.backends.keys())
                }

            return {
                "filepath": filepath,
                "detected_format": detected_format,
                "backend": f"{detected_format} server",
                "status": "ready for analysis"
            }

        @self.gateway.tool()
        def list_all_capabilities() -> dict:
            """List capabilities of all backend servers."""
            capabilities = {}
            for name in self.backends:
                capabilities[name] = {
                    "name": name,
                    "mounted_at": f"/{name}",
                    "status": "active"
                }
            return capabilities

    def get_server(self) -> FastMCP:
        """Get gateway server instance."""
        return self.gateway


# Production usage
if __name__ == "__main__":
    gateway = CompleteCLIOGateway()
    server = gateway.get_server()

    # Run server
    # server.run()  # Production deployment
```

---

## Summary

**Key CLIO-Specific Patterns**:

1. **Gateway Pattern**: Aggregate 15+ servers using `mount()`
2. **Scientific Servers**: Dedicated servers per format (HDF5, Parquet, NetCDF)
3. **Capability Metadata**: Register tools with format/operation metadata
4. **Dynamic Routing**: Route queries based on intent and capabilities
5. **Context Threading**: Propagate context through 3-tier hierarchy
6. **DSPy Integration**: Bridge MCP tools into DSPy reasoning
7. **Resource Configuration**: Use resources for dynamic config
8. **Progress Reporting**: Report progress for long analyses
9. **Graceful Degradation**: Fall back to simpler methods on failure
10. **Complete Examples**: Production-ready gateway and scientific servers

These patterns enable CLIO Agent to orchestrate complex scientific workflows across multiple data formats and compute resources while maintaining clean separation of concerns and robust error handling.
