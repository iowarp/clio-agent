# FastMCP Deployment
> Version: fastmcp 2.x (3.0 beta) | Updated: February 2026

FastMCP servers support multiple transport protocols and deployment strategies, from local stdio to production HTTP/ASGI deployments.

## Table of Contents
- [Transport Options](#transport-options)
- [mcp.run() - CLI Runner](#mcprun---cli-runner)
- [Stdio Transport](#stdio-transport)
- [SSE Transport](#sse-transport)
- [HTTP Transport](#http-transport)
- [ASGI Integration](#asgi-integration)
- [Authentication Patterns](#authentication-patterns)
- [Production Deployment](#production-deployment)
- [Docker Deployment](#docker-deployment)
- [CLIO-Specific Deployment](#clio-specific-deployment)

## Transport Options

FastMCP supports three transport protocols:

1. **stdio** - Standard input/output (local execution, subprocess communication)
2. **sse** - Server-Sent Events (HTTP streaming, real-time updates)
3. **http** - Streamable HTTP (request/response with streaming support)

Transport selection depends on your deployment environment and client requirements.

## mcp.run() - CLI Runner

`mcp.run()` is the primary entry point for running FastMCP servers. It handles transport detection, server lifecycle, and graceful shutdown.

### Auto-Detection

```python
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

@mcp.tool()
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    # Auto-detects transport from environment
    mcp.run()
```

When run directly, `mcp.run()` uses stdio transport by default. When served via HTTP server, it adapts automatically.

### Explicit Transport

```python
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

@mcp.tool()
def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    # Explicitly specify transport
    mcp.run(transport="stdio")
```

### Transport-Specific Configuration

```python
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

@mcp.tool()
def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        # Run as HTTP server
        mcp.run(transport="http", host="0.0.0.0", port=8000)
    elif len(sys.argv) > 1 and sys.argv[1] == "--sse":
        # Run as SSE server
        mcp.run(transport="sse", host="0.0.0.0", port=8001)
    else:
        # Default to stdio
        mcp.run(transport="stdio")
```

## Stdio Transport

Stdio transport uses standard input/output for communication, ideal for local execution and subprocess integration.

### Basic Stdio Server

```python
from fastmcp import FastMCP

mcp = FastMCP(name="Stdio Server")

@mcp.tool()
def process_data(data: str) -> str:
    """Process data locally."""
    return data.upper()

if __name__ == "__main__":
    # Run with stdio transport (default)
    mcp.run()
```

**Usage:**
```bash
# Run directly
python server.py

# Or via client
python -m fastmcp.client ./server.py
```

### Stdio with Logging

```python
from fastmcp import FastMCP
import logging

# Configure logging (stderr to avoid interfering with stdio)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]  # Uses stderr by default
)

mcp = FastMCP(name="Stdio Server with Logging")

@mcp.tool()
async def compute(x: int, ctx) -> int:
    """Compute with logging."""
    await ctx.info(f"Computing with x={x}")
    result = x ** 2
    await ctx.info(f"Result: {result}")
    return result

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Subprocess Integration

```python
# server.py
from fastmcp import FastMCP

mcp = FastMCP(name="Subprocess Server")

@mcp.tool()
def echo(message: str) -> str:
    return message

if __name__ == "__main__":
    mcp.run()

# client.py - calling server as subprocess
import subprocess
import json

def call_mcp_server(tool_name: str, **kwargs):
    """Call MCP server tool via subprocess."""
    proc = subprocess.Popen(
        ["python", "server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": kwargs
        },
        "id": 1
    }

    stdout, stderr = proc.communicate(json.dumps(request))
    return json.loads(stdout)

# Usage
result = call_mcp_server("echo", message="Hello!")
print(result)
```

## SSE Transport

Server-Sent Events (SSE) provides HTTP-based streaming for real-time updates and long-running operations.

### Basic SSE Server

```python
from fastmcp import FastMCP

mcp = FastMCP(name="SSE Server")

@mcp.tool()
async def stream_data(count: int, ctx) -> list[int]:
    """Stream data with progress updates."""
    import asyncio

    results = []
    for i in range(count):
        await ctx.report_progress(progress=i+1, total=count)
        await asyncio.sleep(0.1)  # Simulate work
        results.append(i)

    return results

if __name__ == "__main__":
    mcp.run(transport="sse", host="127.0.0.1", port=8000)
```

**Client Usage:**
```python
from fastmcp.client import Client

async def main():
    async with Client("http://127.0.0.1:8000/sse") as client:
        # Client receives progress updates via SSE
        result = await client.call_tool("stream_data", count=10)
        print(result)
```

### SSE with CORS

```python
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

mcp = FastMCP(name="SSE Server with CORS")

@mcp.tool()
def get_data() -> dict:
    return {"status": "ok"}

if __name__ == "__main__":
    # Add CORS middleware for browser clients
    app = mcp.get_asgi_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    mcp.run(transport="sse", host="0.0.0.0", port=8000)
```

## HTTP Transport

Streamable HTTP transport provides request/response communication with streaming support.

### Basic HTTP Server

```python
from fastmcp import FastMCP

mcp = FastMCP(name="HTTP Server")

@mcp.tool()
def calculate(x: float, y: float) -> float:
    """Perform calculation."""
    return x + y

@mcp.resource("data://{id}")
def get_data(id: str) -> str:
    """Get data resource."""
    return f"Data for {id}"

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)
```

**Client Usage:**
```bash
# List tools
curl http://127.0.0.1:8000/tools/list

# Call tool
curl -X POST http://127.0.0.1:8000/tools/call \
  -H "Content-Type: application/json" \
  -d '{"name": "calculate", "arguments": {"x": 5, "y": 3}}'

# Get resource
curl http://127.0.0.1:8000/resources/read?uri=data://123
```

### HTTP with Custom Host/Port

```python
from fastmcp import FastMCP
import os

mcp = FastMCP(name="HTTP Server")

@mcp.tool()
def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    # Read from environment or use defaults
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))

    mcp.run(transport="http", host=host, port=port)
```

### HTTP with TLS/SSL

```python
from fastmcp import FastMCP
import uvicorn

mcp = FastMCP(name="HTTPS Server")

@mcp.tool()
def secure_operation(data: str) -> str:
    """Secure operation over HTTPS."""
    return f"Processed securely: {data}"

if __name__ == "__main__":
    # Get ASGI app
    app = mcp.get_asgi_app()

    # Run with uvicorn and SSL
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8443,
        ssl_keyfile="/path/to/key.pem",
        ssl_certfile="/path/to/cert.pem"
    )
```

## ASGI Integration

FastMCP servers expose ASGI applications for integration with FastAPI, Starlette, or other ASGI frameworks.

### Basic ASGI Integration

```python
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.responses import JSONResponse

# Create FastMCP server
mcp = FastMCP(name="MCP Server")

@mcp.tool()
def mcp_tool(data: str) -> str:
    return f"Processed: {data}"

# Create Starlette app
app = Starlette()

# Health check endpoint
@app.route("/health")
async def health(request):
    return JSONResponse({"status": "healthy"})

# Mount MCP server at /mcp
app.mount("/mcp", mcp.get_asgi_app())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Access:**
- Health check: `http://localhost:8000/health`
- MCP tools: `http://localhost:8000/mcp/tools/list`

### FastAPI Integration

```python
from fastmcp import FastMCP
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Create FastAPI app
app = FastAPI(title="MCP API Gateway")

# Add CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST API endpoints
@app.get("/api/status")
async def status():
    return {"status": "running"}

@app.get("/api/version")
async def version():
    return {"version": "1.0.0"}

# Create MCP servers
scientific_mcp = FastMCP(name="Scientific Tools")

@scientific_mcp.tool()
def analyze_data(data: list[float]) -> dict:
    """Analyze scientific data."""
    return {
        "mean": sum(data) / len(data),
        "count": len(data)
    }

admin_mcp = FastMCP(name="Admin Tools")

@admin_mcp.tool()
def admin_operation(action: str) -> str:
    """Admin operation."""
    return f"Executed: {action}"

# Mount MCP servers
app.mount("/mcp/scientific", scientific_mcp.get_asgi_app())
app.mount("/mcp/admin", admin_mcp.get_asgi_app())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Access:**
- REST API: `http://localhost:8000/api/status`
- Scientific tools: `http://localhost:8000/mcp/scientific/tools/list`
- Admin tools: `http://localhost:8000/mcp/admin/tools/list`

### Multi-Tenant ASGI

```python
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

class TenantMiddleware(BaseHTTPMiddleware):
    """Extract tenant from request headers."""

    async def dispatch(self, request, call_next):
        tenant_id = request.headers.get("X-Tenant-ID", "default")
        request.state.tenant_id = tenant_id
        response = await call_next(request)
        return response

# Create tenant-specific MCP servers
def create_tenant_server(tenant_id: str) -> FastMCP:
    mcp = FastMCP(name=f"Tenant {tenant_id} Server")

    @mcp.tool()
    def get_tenant_data() -> dict:
        """Get tenant-specific data."""
        return {"tenant": tenant_id, "data": "..."}

    return mcp

# Create main app
app = Starlette(middleware=[Middleware(TenantMiddleware)])

# Mount tenant servers
tenants = ["acme", "globex", "initech"]
for tenant in tenants:
    tenant_server = create_tenant_server(tenant)
    app.mount(f"/mcp/{tenant}", tenant_server.get_asgi_app())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

## Authentication Patterns

FastMCP provides dependency injection for authentication via `Context` and custom dependencies.

### Token-Based Authentication

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import Depends

def verify_token(ctx: Context) -> str:
    """Verify authentication token from context."""
    # In production, extract from ctx headers or client_id
    # For this example, assume token in client_id
    token = ctx.client_id

    if not token or not token.startswith("token_"):
        raise PermissionError("Invalid authentication token")

    # Verify token (simplified)
    return token

mcp = FastMCP(name="Authenticated Server")

@mcp.tool()
async def protected_operation(
    data: str,
    token: str = Depends(verify_token)
) -> str:
    """Protected operation requiring authentication."""
    return f"Authorized: {data}"

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
```

### Role-Based Access Control

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import Depends
from enum import Enum

class Role(Enum):
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"

def get_user_role(ctx: Context) -> Role:
    """Extract user role from context."""
    # Parse from ctx.client_id or custom headers
    role_str = ctx.client_id.split("_")[0] if ctx.client_id else "guest"

    try:
        return Role(role_str)
    except ValueError:
        return Role.GUEST

def require_admin(role: Role = Depends(get_user_role)) -> Role:
    """Require admin role."""
    if role != Role.ADMIN:
        raise PermissionError("Admin access required")
    return role

mcp = FastMCP(name="RBAC Server")

@mcp.tool()
async def public_operation(data: str) -> str:
    """Public operation - no auth required."""
    return f"Public: {data}"

@mcp.tool()
async def user_operation(
    data: str,
    role: Role = Depends(get_user_role)
) -> str:
    """User operation - requires user or admin role."""
    if role == Role.GUEST:
        raise PermissionError("Authentication required")
    return f"User operation: {data}"

@mcp.tool()
async def admin_operation(
    data: str,
    role: Role = Depends(require_admin)
) -> str:
    """Admin operation - requires admin role."""
    return f"Admin operation: {data}"

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
```

### API Key Authentication

```python
from fastmcp import FastMCP, Context
from fastmcp.dependencies import Depends
import os

def verify_api_key(ctx: Context) -> bool:
    """Verify API key from context."""
    # In production, extract from X-API-Key header via ctx
    provided_key = ctx.client_id  # Simplified

    valid_key = os.getenv("MCP_API_KEY", "secret_key_123")

    if provided_key != valid_key:
        raise PermissionError("Invalid API key")

    return True

mcp = FastMCP(name="API Key Server")

@mcp.tool()
async def secure_operation(
    data: str,
    authenticated: bool = Depends(verify_api_key)
) -> str:
    """Operation requiring API key."""
    return f"Secure: {data}"

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
```

## Production Deployment

### Process Manager (systemd)

```ini
# /etc/systemd/system/mcp-server.service
[Unit]
Description=FastMCP Server
After=network.target

[Service]
Type=simple
User=mcpuser
WorkingDirectory=/opt/mcp-server
Environment="PATH=/opt/mcp-server/venv/bin"
Environment="MCP_HOST=0.0.0.0"
Environment="MCP_PORT=8000"
ExecStart=/opt/mcp-server/venv/bin/python server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Commands:**
```bash
# Enable and start
sudo systemctl enable mcp-server
sudo systemctl start mcp-server

# Check status
sudo systemctl status mcp-server

# View logs
sudo journalctl -u mcp-server -f
```

### Reverse Proxy (Nginx)

```nginx
# /etc/nginx/sites-available/mcp-server
upstream mcp_backend {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name mcp.example.com;

    # Redirect to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name mcp.example.com;

    ssl_certificate /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    location / {
        proxy_pass http://mcp_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
    }
}
```

### Load Balancing

```nginx
upstream mcp_cluster {
    least_conn;
    server 127.0.0.1:8000;
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
}

server {
    listen 443 ssl http2;
    server_name mcp.example.com;

    # SSL config...

    location / {
        proxy_pass http://mcp_cluster;
        # Proxy headers...
    }
}
```

## Docker Deployment

### Basic Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose port
EXPOSE 8000

# Run server
CMD ["python", "server.py", "--http"]
```

**requirements.txt:**
```
fastmcp>=2.13.0
uvicorn[standard]>=0.27.0
```

**Build and run:**
```bash
docker build -t mcp-server .
docker run -p 8000:8000 mcp-server
```

### Multi-Stage Dockerfile

```dockerfile
# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --upgrade pip uv

# Copy and install dependencies
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application
COPY src/ ./src/

# Add virtualenv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

EXPOSE 8000

CMD ["python", "-m", "src.server"]
```

### Docker Compose

```yaml
# docker-compose.yml
version: '3.8'

services:
  mcp-server:
    build: .
    ports:
      - "8000:8000"
    environment:
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8000
      - LOG_LEVEL=info
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 3s
      retries: 3

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
    depends_on:
      - mcp-server
    restart: unless-stopped
```

**Run:**
```bash
docker compose up -d
docker compose logs -f mcp-server
```

## CLIO-Specific Deployment

CLIO deploys MCP servers based on the agent tier and operational mode.

### CLIO Main Agent MCP Server

```python
# src/clio_agent/mcp/main_server.py
from fastmcp import FastMCP, Context
from clio_agent.arc import ARCMemory
from clio_agent.config import Config

def create_clio_main_server(config: Config, arc: ARCMemory) -> FastMCP:
    """Create CLIO Main Agent MCP server."""

    mcp = FastMCP(
        name="CLIO Main Agent",
        version=config.version
    )

    @mcp.tool()
    async def query_arc_memory(
        query: str,
        ctx: Context
    ) -> list[dict]:
        """Query ARC memory system."""
        await ctx.info(f"Querying ARC: {query}")

        results = arc.semantic_search(query, limit=10)

        return [
            {
                "content": r.content,
                "score": r.score,
                "timestamp": r.timestamp
            }
            for r in results
        ]

    @mcp.tool()
    async def get_conversation_history(
        session_id: str,
        ctx: Context
    ) -> list[dict]:
        """Get conversation history from ARC."""
        await ctx.info(f"Retrieving history for session {session_id}")

        history = arc.get_conversation_history(session_id)

        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp
            }
            for msg in history
        ]

    return mcp

if __name__ == "__main__":
    # Initialize CLIO
    config = Config.load()
    arc = ARCMemory(config=config)

    # Create and run server
    server = create_clio_main_server(config, arc)
    server.run(transport="stdio")  # Default to stdio for agent integration
```

### CLIO Expert MCP Gateway

```python
# src/clio_agent/mcp/expert_gateway.py
from fastmcp import FastMCP
from clio_agent.experts.data_expert import DataExpert
from clio_agent.arc import ARCMemory

def create_expert_gateway(
    expert: DataExpert,
    arc: ARCMemory
) -> FastMCP:
    """Create gateway for CLIO expert agents."""

    gateway = FastMCP(name=f"CLIO {expert.name} Gateway")

    # Mount domain-specific tool servers
    from clio_agent.mcp.tools import (
        create_hdf5_server,
        create_parquet_server,
        create_iowarp_server
    )

    gateway.mount(create_hdf5_server(), prefix="hdf5")
    gateway.mount(create_parquet_server(), prefix="parquet")
    gateway.mount(create_iowarp_server(), prefix="iowarp")

    # Add expert-specific tools
    @gateway.tool()
    async def invoke_expert(query: str, ctx) -> dict:
        """Invoke expert agent."""
        await ctx.info(f"Invoking expert: {query}")

        result = expert.forward(query)

        # Store in ARC
        arc.store_invocation({
            "expert": expert.name,
            "query": query,
            "result": result
        })

        return {"result": result}

    return gateway

if __name__ == "__main__":
    # For standalone expert server deployment
    from clio_agent.config import Config

    config = Config.load()
    arc = ARCMemory(config=config)
    expert = DataExpert(config=config, arc=arc)

    gateway = create_expert_gateway(expert, arc)
    gateway.run(transport="http", host="0.0.0.0", port=8001)
```

### CLIO Docker Deployment

```dockerfile
# Dockerfile for CLIO Agent
FROM python:3.12-slim

WORKDIR /app

# System dependencies for scientific libraries
RUN apt-get update && apt-get install -y \
    build-essential \
    libhdf5-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy CLIO source
COPY src/ ./src/

# Create data directories
RUN mkdir -p /app/data/arc /app/data/cache

# Expose ports for different components
EXPOSE 8000 8001 8002

# Default: Run main CLI
CMD ["python", "-m", "src.clio_agent.ui.cli"]
```

**docker-compose.yml for CLIO:**
```yaml
version: '3.8'

services:
  clio-main:
    build: .
    command: python -m src.clio_agent.mcp.main_server
    ports:
      - "8000:8000"
    environment:
      - CLIO_MODE=production
      - ARC_STORAGE=/app/data/arc
    volumes:
      - arc-data:/app/data/arc
      - cache-data:/app/data/cache

  clio-expert-data:
    build: .
    command: python -m src.clio_agent.mcp.expert_gateway --expert=data
    ports:
      - "8001:8001"
    environment:
      - CLIO_MODE=production
    volumes:
      - arc-data:/app/data/arc
    depends_on:
      - clio-main

  iowarp-service:
    image: iowarp:latest
    ports:
      - "9000:9000"
    volumes:
      - iowarp-data:/data

volumes:
  arc-data:
  cache-data:
  iowarp-data:
```

## Best Practices

1. **Use Environment Variables**: Configure host, port, and secrets via environment variables.

2. **Health Checks**: Implement health check endpoints for monitoring.

3. **Graceful Shutdown**: FastMCP handles SIGTERM/SIGINT gracefully by default.

4. **Logging**: Use structured logging to stderr for stdio transport, file/external logging for HTTP/SSE.

5. **SSL/TLS**: Always use HTTPS in production with valid certificates.

6. **Rate Limiting**: Implement rate limiting at nginx/proxy layer or via transforms.

7. **Monitoring**: Use Prometheus/OpenTelemetry for metrics and tracing.

8. **Docker**: Use multi-stage builds for smaller images and security.

## Summary

FastMCP deployment options:
- **stdio** for local execution and subprocess communication
- **SSE** for real-time streaming and browser clients
- **HTTP** for standard request/response with streaming support
- **ASGI** for integration with FastAPI/Starlette
- **Authentication** via dependency injection and Context
- **Production** ready with systemd, nginx, Docker support

CLIO uses stdio for local agent operation and HTTP for distributed expert deployment, with all components integrated via the ARC memory system.
