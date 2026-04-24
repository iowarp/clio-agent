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

### SSE streaming

When `stream: true`, the response is `text/event-stream`. Events (`api.py:259-299`):

```
event: routing
data: {"selected_expert": "data"}

event: chunk
data: {"text": "partial answer…"}

event: done
data: {"duration_ms": 1234.5, "selected_expert": "data"}
```

> **Note:** the SSE `chunk` events are currently **composed in the FastAPI layer** (`test_api.py:238-271`) — they're not driven by real token streaming from the agent core. Today the FastAPI wrapper synthesises them from the final answer. True token streaming is Phase 4+.

### Health shape

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

The TUI does **not** need to speak MCP directly — it goes through `/query` and the expert dispatches the tool calls internally. MCP is relevant if the TUI wants to show a "raw tool palette" mode.

## Calling CLIO from the TUI — four options

### A. Subprocess CLI + `--json`

```go
out, _ := exec.Command("clio-agent", "--query", q, "--session", sid, "--json").Output()
```

**Pros:** zero runtime deps. **Cons:** 1–2 s subprocess boot per query.

### B. REST API (recommended for TUI)

```go
resp, _ := http.Post(url+"/query", "application/json",
    strings.NewReader(`{"question":"…","session_id":"…","stream":true}`))
// then SSE-parse if stream=true
```

**Pros:** long-running server, health endpoint, SSE for progress. **Cons:** must manage uvicorn process (solvable via `gact agent deploy`-style adapter).

### C. Direct Python import (same-process)

```python
from clio_agent import ClioAgent
agent = ClioAgent()
result = agent(question=q, session_id=sid)
```

Not useful for a Go TUI; relevant only for a Python wrapper.

### D. MCP client (tool-level)

If the MCP gateway is served over HTTP, the TUI can call individual tools directly (bypassing expert dispatch). Niche — use for a power-user "raw tool" panel.

## Recommended path for gact-tui

**Option B with a thin adapter.** The GACT protocol already models the same primitives (sessions, messages, tool calls, streaming events). The integration work is:

1. Translate GACT `/v1/sessions/{id}/messages` POST → `POST /query` with `session_id` + question.
2. Map CLIO's SSE events (`routing`, `chunk`, `done`) → GACT's `message.part.delta` / `tool.call.started` / `message.completed`.
3. Surface `/experts`, `/health`, `/metrics` behind GACT's `/v1/catalog`, `/v1/health`, `/v1/metrics`.
4. Package the adapter the same way we did `claudecode` (`adapters/claudecode/`) so `gact agent deploy clio my-clio` just works.

Covered in depth in `09-integration-plan.md`.
