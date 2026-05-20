# 06 — Endpoints

> Every surface a TUI can hit today. Source: `src/clio_agent/ui/cli.py`, `ui/api.py`, `pyproject.toml`.

## Console entry points

From `pyproject.toml` (`[project.scripts]`):

```toml
clio-agent     = "clio_agent.ui.cli:run_cli"
clio-agent-api = "clio_agent.ui.api:main"
```

## CLI — interactive + one-shot

Module: `ui/cli.py`. Rich-based TUI (foreground).

### Interactive mode

```
$ clio-agent
```

Slash commands available inside the REPL:

| Command | Purpose |
|---|---|
| `/help` | show all commands |
| `/history` | conversation history |
| `/experts` | list registered experts with keywords |
| `/registry` | Agent Registry status |
| `/memory` | ARC cache statistics |
| `/tools` | MCP tools via gateway |
| `/doctor` | runtime integration health |
| `/metrics` | per-expert performance |
| `/verbose` | toggle reasoning trace display |
| `/clear` | clear history |
| `/quit`, `/exit` | exit |

### One-shot mode

```
$ clio-agent --query "Optimize my HDF5?" [--session SID] [--json] [--verbose]
```

Prints `{"answer","selected_expert","session_id","duration_ms","error_info"}` with `--json`. Handy for scripted integration or testing without a server.

## REST API — `clio-agent-api`

Module: `ui/api.py`. FastAPI + Uvicorn. Default `:8000`.

```
$ clio-agent-api --host 0.0.0.0 --port 8000 [--reload]
```

### Lifecycle

On startup (`api.py:103-138`):
1. `load_config_from_env()` → `LMProviderConfig`
2. `setup_dspy()`
3. `ClioAgent()` → attached to `app.state.agent`
4. `app.state.healthy = True` once init succeeds

On shutdown: `agent.shutdown()` if available.

### Routes

| Method | Path | Body | Response | Status codes |
|---|---|---|---|---|
| **POST** | `/query` | `{"question": str, "session_id": str?, "stream": bool?}` | JSON `QueryResponse` **or** SSE stream | 200, 422 (validation), 500, 503 (degraded) |
| **GET** | `/health` | — | `{status, version, provider, environment, overall_status, integrations[], error?}` | 200 / 503 |
| **GET** | `/experts` | — | `{experts: [{id, description, keywords, tools}]}` | 200 |
| **GET** | `/metrics` | — | `{metrics: {agent_id → Metrics}}` | 200 |

### `QueryResponse`

```json
{
  "answer": "string",
  "selected_expert": "data|analysis|visualization|chat|none",
  "session_id": "string",
  "duration_ms": 1234.5,
  "error_info": null | {"error": "expert_error", "message": "...", "details": {...}}
}
```

### Legacy SSE completion

When `stream: true`, the legacy `/query` response is still
`text/event-stream`, but it is not live token streaming. The API emits
an SSE envelope around a completed answer:

```
event: routing
data: {"selected_expert": "data"}

event: done
data: {"duration_ms": 1234.5, "selected_expert": "data", "stream_source": "synthetic_posthoc"}
```

> **Note:** legacy `/query` no longer emits synthetic `chunk` events.
> The completed answer is labeled with `stream_source="synthetic_posthoc"`
> and `stream_fallback.reason="legacy_query_sync_path"`. Use native GACT
> `/v1/sessions/{sid}/events` for best-effort live provider-token streaming.

This section describes the legacy `clio-agent-api` surface. The primary TUI integration uses the native GACT backend below.

## GACT API — `clio-agent-gact`

Module: `gact/app.py`. FastAPI + Uvicorn. It is a peer of `clio-agent-api`, not a thin translator over `/query`.

```
$ clio-agent-gact --host 127.0.0.1 --port 17800
```

### Core routes

| Method | Path | Purpose |
|---|---|---|
| **GET** | `/v1/health` | backend and integration health |
| **GET** | `/v1/capabilities` | advertised GACT capability flags |
| **GET / POST** | `/v1/sessions` | session list/create |
| **GET / PATCH / DELETE** | `/v1/sessions/{sid}` | session metadata and lifecycle |
| **POST** | `/v1/sessions/{sid}/messages` | enqueue a user turn; response acks quickly |
| **DELETE** | `/v1/sessions/{sid}/messages/{message_id}` | delete one message from a specific session |
| **GET** | `/v1/sessions/{sid}/events` | SSE stream for `message.*`, `tool.call.*`, and session status events |
| **POST** | `/v1/sessions/{sid}/cancel` | best-effort cancellation envelope |
| **GET / PUT** | `/v1/providers/lm` | inspect or hot-swap LM provider config |

### GACT streaming

Clients post the message, then consume `/events`. Live text deltas include `stream_source` so the TUI can distinguish live token/proxy streaming from post-hoc text delivery:

| `stream_source` | Meaning |
|---|---|
| `live` | delta arrived through the live `dspy.streamify` path |
| `synthetic_posthoc` | final answer was already available before live provider-token deltas could be emitted |

Synthetic post-hoc payloads include a structured `stream_fallback`
object with `reason`, `category`, `description`, `recovery_actions`,
`synthetic_posthoc=true`, and `live_streaming=false`. The allowed reason
catalog is advertised in `/v1/capabilities` as
`capabilities.x_clio_stream_fallback_reasons`.
Provider/planner failures during live stream execution settle the turn
with structured `error_info` instead of falling back to synthetic answer
text.

Cancellation is also explicit rather than hidden. Cancelling a running turn settles the GACT envelope as cancelled; if provider/tool work is already inside an executor thread, `session.status_changed` marks `execution_cancellation="best_effort"` and `executor_work_may_continue=true`.

## Legacy REST health shape

```json
{
  "status": "ok" | "degraded",
  "version": "0.2.0",
  "provider": "lm_studio|ollama|openai|anthropic",
  "environment": "dev|staging|production",
  "overall_status": "ready|degraded|unavailable",
  "integrations": [
    {"name": "lm",       "status": "ready",       "detail": "..."},
    {"name": "gateway",  "status": "ready",       "detail": "..."},
    {"name": "arc",      "status": "ready",       "detail": "..."},
    {"name": "clio_core","status": "unavailable", "detail": "..."}
  ],
  "error": null
}
```

## Future endpoints (v0.4+)

Tracked in `PLAN.md:149-150, 339-350`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/task/submit` | long-running task |
| GET | `/task/{id}/status` | task progress + artifacts |
| DELETE | `/task/{id}/cancel` | cancel task |
| GET | `/artifacts/{id}` | generated plots / reports |
| POST/GET | `/a2a` | A2A agent-delegation surface |

## MCP gateway

`from clio_agent.tools.gateway import gateway` → `FastMCP("clio-gateway")` with 8 tools (see `05-tools.md`). Not bound to an HTTP transport by default. Can be exposed:

```python
# In-process (tests):
async with Client(gateway) as client:
    await client.call_tool("hdf5_analyze_file", {"filepath": "/tmp/x.h5"})

# HTTP (production):
app = gateway.http_app()
uvicorn.run(app, host="0.0.0.0", port=8001)
```

The TUI does **not** need to speak MCP directly — it goes through the GACT message endpoint and the expert dispatches tool calls internally. MCP is relevant if the TUI wants to show a "raw tool palette" mode.

## Calling CLIO from the TUI — options

### A. Native GACT backend (recommended)

```sh
clio-agent-gact --host 127.0.0.1 --port 17800
GACT_BACKEND=http://127.0.0.1:17800 gact
```

**Pros:** first-class `/v1` contract, native sessions, SSE event bus, best-effort cancellation, LM provider configuration, and explicit streaming provenance. **Cons:** runs the CLIO Python backend beside the Go TUI.

### B. Subprocess CLI + `--json`

```go
out, _ := exec.Command("clio-agent", "--query", q, "--session", sid, "--json").Output()
```

**Pros:** zero runtime deps. **Cons:** 1–2 s subprocess boot per query.

### C. Legacy REST API

```go
resp, _ := http.Post(url+"/query", "application/json",
    strings.NewReader(`{"question":"…","session_id":"…","stream":true}`))
// then SSE-parse if stream=true
```

**Pros:** long-running server, health endpoint, simple query response.
**Cons:** old `/query` SSE shape; it only returns a completed answer
with synthetic post-hoc provenance and must not be presented as live
token streaming.

### D. Direct Python import (same-process)

```python
from clio_agent import ClioAgent
agent = ClioAgent()
result = agent(question=q, session_id=sid)
```

Not useful for a Go TUI; relevant only for a Python wrapper.

### E. MCP client (tool-level)

If the MCP gateway is served over HTTP, the TUI can call individual tools directly (bypassing expert dispatch). Niche — use for a power-user "raw tool" panel.

## Recommended path for gact-tui

Use `clio-agent-gact` directly. It already exposes the GACT primitives gact-tui expects: sessions, messages, event streams, cancellation, catalog, provider config, metrics, permissions, hooks, context files, diffs, tasks, and workspaces. Remaining truth gaps are tracked in `REAL_GAPS.md`.

Covered in depth in `09-integration-plan.md`.
