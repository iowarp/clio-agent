# 09 — Integration Plan: gact-tui ↔ clio-agent

> How the GACT TUI (`tui/gact`) becomes CLIO's frontend. Concrete adapter shape, protocol mapping, gaps, and a phased rollout.

## Goal

Let an operator run:

```sh
gact agent deploy clio my-clio
gact connect my-clio
```

…and land in the GACT TUI conversation view against a live CLIO agent — same UX as `claudecode`, just talking to CLIO instead.

## Architecture sketch

```
┌─────────────┐   GACT v0.1 (REST + SSE)   ┌──────────────────┐   in-process    ┌──────────────┐
│  gact TUI   │ ───────────────────────▶   │  clio adapter    │ ──────────────▶ │ ClioAgent    │
│ (Bubbletea) │ ◀──── SSE events ─────     │  (Go, FastAPI    │                  │ (Python)     │
└─────────────┘                            │   or Python bin) │ ◀── MCP tools ──│              │
                                           └──────────────────┘                  └──────────────┘
```

Two shapes to choose between (see "Adapter topology" below). Both sit inside `adapters/clio/` in this repo.

## Protocol mapping: GACT ↔ CLIO

| GACT primitive | CLIO equivalent | Gap / notes |
|---|---|---|
| `POST /v1/sessions` | synthesise locally (store `{sid: uuid, title, …}` in adapter memory) | CLIO doesn't issue sessions; the adapter owns them |
| `GET /v1/sessions` | keep adapter state; no CLIO endpoint | Optional persistence in `~/.config/gact/clio-sessions.json` |
| `POST /v1/sessions/{sid}/messages` | `POST /query {question, session_id}` | 1:1 map; the adapter holds the SSE open during the turn |
| `GET /v1/sessions/{sid}/events` (SSE) | translate CLIO's `/query?stream=true` events (`routing`, `chunk`, `done`) → GACT `message.part.*` / `tool.call.*` / `message.completed` | Routing event → synthetic "thinking" part; chunk → text delta (note: CLIO's chunks are synthesised, not true token stream) |
| `GET /v1/catalog/agents` | `GET /experts` | Direct map: `{id, description, keywords, tools}` |
| `GET /v1/catalog/tools` | enumerate FastMCP gateway | Or proxy `GET /experts[].tools` |
| `GET /v1/health` | `GET /health` | Direct map (provider, environment, integrations array) |
| `GET /v1/metrics` | `GET /metrics` | Direct map per agent |
| `POST /v1/sessions/{sid}/cancel` | — | **Gap**: CLIO has no cancel hook. Adapter can cancel the HTTP request; CLIO will finish the current expert turn server-side. Document as best-effort. |
| `POST /v1/sessions/{sid}/messages/{id}/diffs/apply` | — | **Gap**: no file-diff part in CLIO. Not relevant (CLIO's tools mutate files directly via `hdf5_optimize` etc.). |
| Permission flow | — | **Gap**: CLIO guards at the file-policy layer (`CLIO_ALLOWED_ROOTS`) rather than a per-call permission prompt. Adapter surfaces violations as a `tool_result` error; TUI renders as a warning. |
| Workspace / context files | — | **Gap**: CLIO has `DatasetProfile` as the closest analogue. Defer; expose as read-only in v1 of the adapter. |

## SSE event translation table

When the adapter consumes `POST /query?stream=true` from CLIO and re-emits GACT events:

| CLIO SSE | → | GACT event | Notes |
|---|---|---|---|
| `routing {selected_expert}` | → | `message.part.added` (type=`thinking`, text=`"Routing to {expert}…"`) | First event of turn; lets TUI paint the expert badge |
| `chunk {text}` | → | `message.part.delta` (append to a single text part) | Collapse into one part so the TUI sees continuous streaming |
| `done {duration_ms, selected_expert}` | → | `message.completed` + `session.status_changed` to `idle` | Attach `duration_ms` via GACT metadata |
| (none) | → | `tool.call.started` / `tool.call.completed` | **Gap**: CLIO doesn't emit per-tool events over SSE. Fetch `Invocation.tools_called` via a post-turn `GET /invocations` (future endpoint) or leave blank. |
| `error {...}` | → | `message.completed` with `error_info` + status `idle` | CLIO's structured error shape already matches GACT's; pass through. |

## Adapter topology choices

### Option A — Go subprocess manager + Python REST consumer **(recommended)**

`adapters/clio/` is a Go binary (like `claudecode`):
- On startup, spawns `clio-agent-api --host 127.0.0.1 --port <free>` as a subprocess.
- Exposes GACT-conformant REST + SSE on its own port.
- Translates requests into CLIO REST calls + SSE streams into GACT events.
- On shutdown, SIGTERMs CLIO.

**Pros:** matches the `claudecode` pattern 1:1, works with `gact agent deploy`, no Python runtime in the TUI.

**Cons:** two processes per session; marshalling overhead (negligible — ~10 ms per message).

### Option B — Pure Python binary exposing GACT directly

`adapters/clio/` is a Python binary that implements GACT v0.1 natively and imports `ClioAgent` in-process.

**Pros:** one process, zero marshalling, sub-10ms dispatch.

**Cons:** requires `uv`/`python ≥ 3.12` on the operator's box at runtime; can't share code with other Go adapters; harder to package in `gact agent deploy`.

**Decision:** Option A. Mirrors `claudecode`, keeps the deploy UX identical, accepts the trivial subprocess cost.

## Adapter internals — proposed file layout

```
adapters/clio/
├── go.mod
├── cmd/
│   └── gact-clio-adapter/main.go    # binary entry point, CLI flags
├── server.go                         # GACT HTTP server (chi/echo/mux)
├── subprocess.go                     # spawn + supervise clio-agent-api
├── translate.go                      # CLIO ↔ GACT protocol mapping
├── sessions.go                       # adapter-owned session registry
└── smoke_test.go                     # requires `clio-agent` on PATH or CI mark
```

Interfaces (sketch):

```go
type ClioClient struct {
    endpoint string  // http://127.0.0.1:8000 owned by our subprocess
    http     *http.Client
}

func (c *ClioClient) Query(ctx context.Context, req QueryReq) (<-chan SSEvent, error)
func (c *ClioClient) Health(ctx context.Context) (HealthResp, error)
func (c *ClioClient) Experts(ctx context.Context) ([]Expert, error)
func (c *ClioClient) Metrics(ctx context.Context) (map[string]Metrics, error)
```

The translator consumes `<-chan SSEvent` and emits GACT `events.Event` values onto the session's bus.

## Deployment — `gact agent deploy clio my-clio`

Extend `tui/main.go`'s `runAgentDeploy` dispatcher with a `kind == "clio"` branch:

1. Probe for `clio-agent-api` on `PATH` (or `uv run src/clio_agent/ui/api.py`).
2. Pick a free port via `net.Listen(":0")`.
3. Spawn the adapter binary (`adapters/clio/cmd/gact-clio-adapter`) with `--clio-endpoint http://127.0.0.1:{port}` and `--listen :{adapter_port}`.
4. Adapter binary itself spawns `clio-agent-api`; supervises it.
5. Probe `GET /v1/capabilities` on the adapter port up to 3 s — same pattern as `claudecode`.
6. Record `(name, kind=clio, bin, host, port, pid, cwd)` in `~/.config/gact/agents.json`.

`gact connect my-clio` already works once the registry entry exists.

## Configuration pass-through

Environment the user sets before `gact agent deploy`:

| User sets | Adapter forwards to subprocess |
|---|---|
| `CLIO_LM_PROVIDER`, `CLIO_LM_API_BASE`, `CLIO_LM_MODEL`, `CLIO_LM_API_KEY` | via `os.Environ` |
| `CLIO_ALLOWED_ROOTS` | same |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | same (for cloud providers) |

TUI Settings panel could eventually drive these (Settings → "Clio backend" tab), but v1 just honours the env.

## What to build first (phased plan)

### Phase 1 — smoke path (~1 week)

- [ ] `adapters/clio/` skeleton (Go, mirroring `claudecode/`).
- [ ] `clio-agent` detection + spawn + port probing.
- [ ] `POST /v1/sessions` + `POST /v1/sessions/{sid}/messages` → `/query` (non-streaming).
- [ ] `GET /v1/health` → CLIO `/health`.
- [ ] Minimal `GET /v1/sessions/{sid}/events` SSE that emits `message.completed` on `/query` return.
- [ ] Smoke test requires `clio-agent` on PATH (skip otherwise).
- [ ] `gact agent deploy clio my-clio && gact connect my-clio` renders a single-turn round-trip.

### Phase 2 — streaming + experts (~1 week)

- [ ] Proxy CLIO's `/query?stream=true` SSE → GACT events (routing / chunk / done / error).
- [ ] `GET /v1/catalog/agents` → `/experts`.
- [ ] `GET /v1/catalog/tools` → gateway tool listing.
- [ ] Expert badge renders in TUI conversation pane.
- [ ] Routing decision surfaced in the thinking block.

### Phase 3 — ARC + metrics (~3 days)

- [ ] `GET /v1/metrics` → CLIO `/metrics`.
- [ ] Show ARC cache hit rate in TUI footer / Settings.
- [ ] Render tool_calls post-hoc from `Invocation.tools_called` (needs new CLIO endpoint OR best-effort inference).
- [ ] `/doctor` view mirrors CLIO's.

### Phase 4 — upstream contributions to CLIO (~ongoing)

These are **gaps** that a better adapter needs upstream work to fill:

- [ ] **Per-tool SSE events** — CLIO currently only emits routing/chunk/done. Emitting `tool.started` / `tool.completed` makes the TUI live instead of post-hoc.
- [ ] **Token streaming** — CLIO's `chunk` events are synthesised. Real token streaming (pass-through from `dspy.LM`) would light up the TUI mid-turn.
- [ ] **Cancellation** — `/task/{id}/cancel` (Phase 4 of CLIO's own roadmap).
- [ ] **Session list / delete** — `GET /sessions`, `DELETE /sessions/{sid}` so the adapter doesn't need to own registry state.
- [ ] **Artifacts** — `/artifacts/{id}` for plots + reports (already on CLIO's plan).

## Testing

- **Unit** — adapter translator round-trip: stub CLIO endpoint, assert GACT events are emitted correctly for routing / chunk / done / error.
- **Integration** — requires `clio-agent` on PATH. Smoke test: deploy adapter, send `POST /query` with `"what datasets are in /tmp/x.h5"` (pre-populated fixture file), assert `selected_expert=="data"` in the SSE stream.
- **Conformance** — run `contract/conformance` against the adapter port. Expect `permission`, `diff`, `context` suites to be marked "unsupported"; everything else should pass.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| CLIO startup takes >3 s (cold LM load) | Extend probe to 10 s for CLIO; surface "starting CLIO…" hint in TUI |
| LM provider misconfig crashes CLIO at `__init__` | Adapter proxies `/health`; if `overall_status=="unavailable"`, TUI shows a Settings CTA instead of an empty chat |
| No tool-level events means the TUI can't live-render tools | Best-effort: render a spinner during `chunk` events; fetch `Invocation.tools_called` on `done` and back-fill the conversation |
| CLIO Python version (3.12+) not available | `gact agent deploy` probes `python3 --version`; hard-fail with install hint |
| Multi-turn context drops after ~4K tokens | Not the TUI's problem to solve; surface in the Settings / Doctor panel so operators know |

## Deliverables for this integration

1. `adapters/clio/` directory shipped in this repo, built from the same Makefile that handles `adapters/claudecode/`.
2. Extension to `tui/main.go` `runAgentDeploy` / `runAgentConnect` for `kind=="clio"`.
3. Smoke test: `make test` includes an adapter/clio path that skips when `clio-agent` isn't installed.
4. Docs on `README.md` under "Supported agents" with a "Clio" row.
5. VHS screenshot: `screenshots/clio-integration.png` — TUI conversation pane showing the DataExpert badge + an HDF5 analyse-file turn.

## Out of scope (for now)

- Python-native adapter (Option B) — revisit if `gact agent deploy`'s Go subprocess approach proves painful.
- A2A agent discovery (future CLIO v0.8+).
- Running the MCP gateway as a first-class peer — the adapter keeps tool calls funneled through `/query` for simplicity.
- TUI-side SIMBA tuning affordance (Phase 3+ of CLIO; probably a Settings button eventually).
