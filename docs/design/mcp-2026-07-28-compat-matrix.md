# MCP 2026-07-28 compatibility matrix

CLIO upgrades to the released FastMCP 4 beta line that negotiates MCP
`2026-07-28`. The dependency set is:

| Package | Constraint | Resolution proved by the migration spike |
|---|---|---|
| `fastmcp` | `==4.0.0b1` | `fastmcp-slim==4.0.0b1` |
| `fastmcp-tasks` | `==4.0.0b1` | SEP-2663 task extension |
| `mcp` | transitive | `2.0.0` |
| `mcp-types` | transitive | `2.0.0` |
| `fastapi` | `>=0.140` | `0.141.1` in the spike |
| `pydantic` | `>=2.12,<2.14` | `2.12.3` in `uv.lock` (the `<2.14` cap keeps the resolver off `2.14.0aN`; the spike's dry-run had floated `2.13.4`, but the committed lock pins `2.12.3`) |
| `dspy` | `==3.3.0b1` | unchanged and compatible |

## Breaking API changes

| FastMCP 3 usage | FastMCP 4 migration | CLIO disposition |
|---|---|---|
| `FastMCP.as_proxy(Client(target))` | `fastmcp.server.create_proxy(target)` | Migrated in the gateway and test proxy factories. The gateway hands the **transport** (not a prebuilt `Client`) to `create_proxy` so the backend leg MIRRORS each front request's negotiated protocol era (a prebuilt `Client` pins the backend to `auto` and would let a legacy front cross to a modern backend). The execution-handler slot (#1106) is preserved via a per-request `client_factory` that rebuilds a dispatcher-carrying client and mirrors the front era. |
| `FastMCP.mount(server, prefix=name)` | `FastMCP.mount(server, namespace=name)` | The dead `prefix=` fallback was deleted; CLIO now calls `namespace=` directly. |

The remaining CLIO MCP surface survives unchanged: `Client` and `FastMCP`
imports, tool decorators, in-memory clients, stdio/HTTP/SSE transports, tool
listing and calls, result views, and client handler keyword arguments. MCP 2.0
does not export a supported-versions list; acceptance checks the connected
client's `protocol_version` attribute.

## Legacy compatibility verdict

The spike ran each direction over stdio in separate pinned virtual environments:

| Client | Server | Negotiated version | Tool result | Verdict |
|---|---|---|---|---|
| FastMCP 4.0.0b1 | FastMCP 4.0.0b1 | `2026-07-28` | `{"echo":"hi"}` | Green |
| FastMCP 4.0.0b1 | FastMCP 3.2.4 | `2025-11-25` | `{"echo":"hi"}` | Green; graceful downgrade |
| FastMCP 3.2.4 | FastMCP 4.0.0b1 | Not observable on the old client | `{"echo":"hi"}` | Green; the new server accepts the legacy handshake |
| FastMCP 3.2.4 | FastMCP 3.2.4 | Not observable on the old client | `{"echo":"hi"}` | Green baseline |

The new client first probes `server/discover` when connecting to a legacy
server. FastMCP 3.2.4 logs non-fatal Pydantic validation warnings to stderr,
then the client falls back to the legacy initialize handshake and the tool call
succeeds. The in-process compatibility tests cover new/new negotiation and the
new server's legacy-handshake path; the module docstring records the
process-isolated stdio evidence.

## Dependency ripple

FastMCP 4 requires Starlette `>=1.0.1`; the former FastAPI floor capped
Starlette below that line. Raising FastAPI to `>=0.140` removes the incompatible
cap. The focused GACT MCP/SSE/stream suite exercises the application and SSE
routes on Starlette 1.x.

Importing `fastmcp-tasks` registers its client extension automatically. A plain
`Client` calling a `task=True` tool transparently polls and returns the final
`CallToolResult`; no task-specific client code is required. The default
`memory://` docket is suitable only for an in-process or single-process test/dev
server. Distributed or persistent task execution requires a Redis/Valkey docket
and a worker.

## claude-agent-sdk on the mcp-2 core (#1107, owner decision)

The Claude Agent SDK declares a protective `mcp<2` bound (added deliberately in
0.2.96) for its SDK-MCP-server bridging feature. CLIO uses the SDK **purely as
an LLM provider** (`ClaudeSDKClient`/`ClaudeAgentOptions`; owner: "to us he is
a provider of llm, not a tool calling semantics") and never touches that
bridging surface. The `[tool.uv] override-dependencies = ["mcp>=2.0.0"]` entry
neutralizes the bound so the `claude-code` extra co-resolves with the mcp-2
core. Verified: claude-agent-sdk 0.2.128 + mcp 2.0.0 + fastmcp 4.0.0b1 in one
environment; full import surface (core symbols + internal query module) clean;
the typed absence seam (`require_claude_agent_sdk`) remains for uninstalled
setups. The live SDK-session smoke runs at the P0 phase gate. Drop the
override when upstream ships mcp-2 support.
