# Threads B + A: ARC live context plane on clio-core CTE, exposed over gact REST/TUI

Worktree: `/home/jcernuda/clio-arc-live-plane`

This is the synthesized implementation plan for two threads that land independently but
share one product goal: persist the ARC live context plane on the clio-core CTE
(Convergent Tiered Environment) store (Thread B), and surface that plane — its state and
its mutation ops — over the gact REST API / TUI event stream (Thread A).

Both threads were grounded against the live code and a verified end-to-end CTE probe. The
file/line anchors below were re-confirmed against the current tree before writing this plan.

---

## Thread B — `CTEStore` (ARCStore over clio-core CTE) + `make_arc_store` factory

### B.0 Verdict: CTE runs IN-PROCESS. No external daemon.

`chimaera_init(ChimaeraMode.kClient, default_with_runtime=True)` self-starts an **embedded
runtime inside the calling Python process** (the env switch is `CHI_WITH_RUNTIME=1`). The
live probe confirmed: it printed `StartLocalServer ... at 127.0.0.1:9414`, loaded the
`clio_cte_core` / `clio_cae_core` / `clio_bdev` modules, created pool `cte_main` and DRAM
bdev `ram::cte_ram_tier1`, and ran PutBlob/GetBlob/BlobQuery/DelBlob successfully **with no
`clio_run` / `chimaera` daemon before, during, or after** (`pgrep` clean). The runtime dies
with the interpreter; it leaves dangling symlinks under `/tmp/chimaera_<user>/` and possibly
`/dev/shm` segments which the *next* init auto-reaps (`ClearUserIpcs` / `WreapAllIpcs`).

Consequence: **no service-management, no docker, no port to health-check.** The store is a
library that boots a co-process. This is the opposite of the stale `IOWarpCTEBackend`
(ZeroMQ REQ to `tcp://localhost:5555`), which assumes an external daemon that does not exist.

The canonical `server.py` reference is written against an **older binding API**
(`MemContext`, `RegisterTarget`, `PoolQuery.Local()`, generated YAML, a `mctx` arg on every
call). In `iowarp-core==2.1.0` all of that is obsolete: `MemContext` does not exist, no
target registration is needed (the bundled DRAM bdev auto-registers), and `Tag`/`Client`
methods take **no `mctx` argument**. Do not port `server.py`; follow B.2 below.

### B.1 Where it plugs in (the seam already exists)

The persistence seam is already cut. `src/clio_agent/arc/storage.py` defines the
`ARCStore` Protocol (`put/get/exists/scan/delete/clear`, lines 46–81), the default
`LocalFSStore` (lines 84–134), and `ARC_KINDS` (lines 34–43, the single source of truth for
record families — note `segments` is already in the tuple). `ARCMemory.__init__` accepts
`store: ARCStore | None = None` and falls back to `LocalFSStore(self.data_dir)`
(`memory.py:76` and `memory.py:94`); `SegmentStore(self._store)` is built on it
(`memory.py:101`). Every record kind is read/written through `self._store.*` — no call site
touches the filesystem directly.

So Thread B is purely additive: add a `CTEStore` class + a `make_arc_store(...)` factory in
`storage.py`, and call the factory at the single `ARCMemory` construction site in `agent.py`.
The `IOWarpCTEBackend` ZeroMQ class is **dead** and should be deleted in this thread (it is
referenced nowhere outside `storage.py` itself — grep confirms only the class def + a
docstring mention).

### B.2 `CTEStore` class shape

A single class implementing the `ARCStore` Protocol over the in-process CTE binding. Mapping:
`kind` → CTE **tag name**; `name` → CTE **blob name** inside that tag. Flat namespace, no
`/` composition — each ARC kind is its own tag, blob names are the record stems. Binary
payloads (ARC stores msgpack) **must be base64-wrapped**: CTE's `Tag.GetBlob` UTF-8-decodes
inside the C++ binding and raises `UnicodeDecodeError` on any non-UTF-8 byte (verified with
`0x83`/`0x81`). base64-on-put / base64-decode-on-get is round-trip-identical for arbitrary
bytes (cost: ~33% DRAM inflation; acceptable).

```python
class CTEStore:
    """ARCStore backed by the in-process clio-core CTE runtime.

    Maps (kind, name) -> (CTE tag, CTE blob). msgpack payloads are base64-wrapped
    because CTE's GetBlob UTF-8-decodes in the binding. The runtime is embedded
    in this process (chimaera_init(..., default_with_runtime=True)) and dies with
    the interpreter; there is NO external daemon.
    """

    _initialized = False          # process-global init guard (class attr)
    _init_lock = threading.Lock()

    def __init__(self, *, config_path: str = "", log_level: str = "error",
                 init_settle_s: float = 0.5) -> None:
        self._ensure_runtime(config_path, log_level, init_settle_s)
        import clio_cte_core_ext as cte
        self._cte = cte
        self._client = cte.get_cte_client()

    @classmethod
    def _ensure_runtime(cls, config_path, log_level, settle_s) -> None:
        with cls._init_lock:
            if cls._initialized:
                return
            os.environ.setdefault("CTP_LOG_LEVEL", log_level)
            import iowarp_core            # MUST precede clio_cte_core_ext: RTLD_GLOBAL
                                          # preload of transitive .so + seeds ~/.clio/clio.yaml
            import clio_cte_core_ext as cte
            # Silence the C++ fd-level startup logging during init.
            devnull = os.open(os.devnull, os.O_WRONLY); saved = os.dup(2)
            os.dup2(devnull, 2)
            try:
                cte.chimaera_init(cte.ChimaeraMode.kClient, True)   # True => embedded runtime
                time.sleep(settle_s)                                # let it spin up (matches tests)
                cte.initialize_cte(config_path, cte.PoolQuery.Dynamic())  # "" => ~/.clio/clio.yaml
            finally:
                os.dup2(saved, 2); os.close(saved); os.close(devnull)
            cls._initialized = True

    # ---- ARCStore Protocol ----
    def put(self, kind, name, data, *, tier="warm"):
        self._cte.Tag(kind).PutBlob(name, base64.b64encode(data), 0)
        # `tier` is advisory: default config is a single DRAM tier, so a follow-up
        # ReorganizeBlob(name, score) is a no-op today. Map tier->score only when a
        # file/HDD bdev is configured (warm=1.0 ... archive=0.0). Skip for now.

    def get(self, kind, name):
        t = self._cte.Tag(kind)
        sz = t.GetBlobSize(name)            # 0 for missing (no raise)
        return None if sz == 0 else base64.b64decode(t.GetBlob(name, sz, 0))

    def exists(self, kind, name):
        return self._cte.Tag(kind).GetBlobSize(name) > 0   # cheap, no data copy

    def scan(self, kind, prefix=""):
        t = self._cte.Tag(kind)
        for n in t.GetContainedBlobs():
            if n.startswith(prefix):
                yield n, self.get(kind, n)
        # Engine-side alternative: self._client.BlobQuery(kind, f"{re.escape(prefix)}.*",
        # 0, PoolQuery.Dynamic()) -> [(tag, blob), ...] (full-string regex; anchor with .*).
        # Prefer the Python-side filter for correctness parity with LocalFSStore.scan.

    def delete(self, kind, name):
        t = self._cte.Tag(kind)
        self._client.DelBlob(t.GetTagId(), name)   # Tag has no per-blob delete; use Client+TagId

    def clear(self):
        for kind in ARC_KINDS:
            t = self._cte.Tag(kind); tid = t.GetTagId()
            for n in t.GetContainedBlobs():
                self._client.DelBlob(tid, n)
```

Notes the implementer must honor (verified, non-obvious):
- **`PutBlob(name, base64.b64encode(data), 0)`** — takes raw bytes; the 3rd arg is offset 0.
- **`GetBlob(name, sz, 0)` returns `str`** (ascii base64), hence `b64decode`. `GetBlobSize`
  returns `0` (not raise) for a missing blob — that is the `exists`/`get`-miss signal.
- **`delete`/`clear` go through the Client + `GetTagId()`**, not the Tag (no per-blob delete
  on Tag; `CteOp.kDelTag` exists but is not bound as a Client method, so `clear` iterates).
- **`Tag(name)` is cheap** (GetOrCreateTag) and can be reconstructed per call; don't cache it.
- `delete` should be a no-op-safe overwrite of the Protocol's "no-op if absent" contract —
  `DelBlob` on a missing blob returns `False` and does not raise, which satisfies it.

### B.3 `make_arc_store(config)` factory

A single factory that returns the configured backend, defaulting to CTE with a LocalFS
fallback. Lives in `storage.py` next to the classes.

```python
def make_arc_store(
    *,
    backend: str | None = None,
    data_dir: str | Path = ".clio_agent/arc",
    config_path: str = "",
) -> ARCStore:
    """Build the ARC persistence backend.

    backend selection (first match wins):
      1. explicit `backend` arg ("cte" | "local")
      2. env CLIO_ARC_STORE ("cte" | "local")
      3. default: "cte" (clio-core CTE, in-process) -> fall back to LocalFSStore on
         ANY import/init failure, with a one-line warning. Durable across this thread
         only while the runtime is up (DRAM-tier); LocalFS is the durable fallback.
    """
    choice = (backend or os.environ.get("CLIO_ARC_STORE", "cte")).strip().lower()
    if choice == "local":
        return LocalFSStore(data_dir)
    if choice == "cte":
        try:
            return CTEStore(config_path=config_path)
        except Exception as exc:   # ImportError (binding absent) or runtime init failure
            warnings.warn(f"CTE store unavailable ({exc}); falling back to LocalFSStore",
                          RuntimeWarning, stacklevel=2)
            return LocalFSStore(data_dir)
    raise ValueError(f"unknown CLIO_ARC_STORE {choice!r}; expected 'cte' or 'local'")
```

**Config key:** `CLIO_ARC_STORE` (env), values `cte` (default) | `local`. This matches the
existing `CLIO_*` env-config convention in `config.py` (e.g. `CLIO_LM_PROVIDER`,
`CLIO_ENVIRONMENT`). Optional `CLIO_ARC_STORE_CONFIG` → CTE `config_path` (empty ⇒
`~/.clio/clio.yaml`, auto-seeded). No new YAML, no new config object — keep it env-driven
like the rest of the provider config.

**Graceful degradation is mandatory** (CLAUDE.md "IOWarp unavailable -> file-based ARC
storage"): a missing binding, a failed `chimaera_init`, or any init exception falls back to
`LocalFSStore` with a warning, never a crash. The fallback is also the **durability** answer:
the default CTE config is a single **DRAM** tier (`ram::cte_ram_tier1`, ~80% RAM) that is
shared memory, **not persisted to disk** — data lives only while the embedded runtime is up.
For cross-restart durability you'd add a `file` bdev to the config and
`chimaera_init(..., is_restart=True)` (WAL replay); that is out of scope for this thread.

### B.4 Wiring into `ARCMemory`

One edit, at the single construction site. `agent.py:202`:

```python
# before
self.arc = ARCMemory(data_dir=f"{data_dir}/arc", cache_capacity=1000)
# after
self.arc = ARCMemory(
    data_dir=f"{data_dir}/arc",
    cache_capacity=1000,
    store=make_arc_store(data_dir=f"{data_dir}/arc"),
)
```

`ARCMemory.__init__` already takes `store=` and threads it into `SegmentStore`
(`memory.py:76/94/101`), so **no change to `memory.py` is required** for B. Every other
`ARCMemory()` call site (tests, `optimizer/*`, `arc/retrieval.py`, `arc/coordinator.py`,
docstrings) keeps the LocalFS default by passing no `store=` — they are unaffected. The init
guard (`CTEStore._initialized` class attr behind a lock) means even if multiple `ARCMemory`
instances are built in one process, `chimaera_init` runs exactly once.

### B.5 B caveats (carry to the implementer)

- **Import order is load-bearing:** `import iowarp_core` BEFORE `import clio_cte_core_ext`.
  `iowarp_core._setup()` does the `RTLD_GLOBAL` `.so` preload (Python's default `RTLD_LOCAL`
  hides symbols and the import fails) and seeds `~/.clio/clio.yaml`. Both imports are inside
  `_ensure_runtime` so the cost is paid only when CTE is actually selected.
- **Init exactly once per process** behind the lock+flag. A second `kClient,True` init in the
  same live process is undefined. `get_cte_client()` returns a copy of the global client;
  store it on the instance.
- **Treat init as single-threaded.** The C++ runtime is internally multi-worker, but serialize
  the Python init. CRUD after init is fine concurrently (each `Tag(name)` is independent).
- **No Python `shutdown()`/`finalize` symbol** is exposed; teardown is on interpreter exit.
  Do not write a `__del__` that calls into the binding. Leftover symlinks/shm are reaped by
  the next init.
- **`CTP_LOG_LEVEL=error`** quiets the very chatty C++ INFO logging; the `dup2(devnull, 2)`
  during init suppresses the fd-level startup banner. Keep both.
- **`tier=` is advisory today** — the Protocol keeps the param for API stability, but with a
  single DRAM tier `ReorganizeBlob` is a no-op. Don't fake tiering; wire it only when a real
  file/HDD bdev lands in the config.

### B.6 Tests for B (must pass; no skips)

- Unit (no binding): `make_arc_store(backend="local")` returns `LocalFSStore`;
  `make_arc_store(backend="bogus")` raises `ValueError`; with the binding import monkeypatched
  to raise, `backend="cte"` falls back to `LocalFSStore` and warns.
- Round-trip (binding present, marked `integration`): `put/get/exists/scan/delete/clear` over
  a `CTEStore`, including a **binary-hostile payload** `msgpack({...})` containing
  `b"\x00\x83\xff\x81"` — assert `get` returns identical bytes (this is the base64 regression
  guard). `scan(prefix=...)` parity vs `LocalFSStore`. Mark `integration` so the unit lane
  (`-m "not integration"`) stays binding-free.
- Wiring: `ARCMemory(store=CTEStore())._store is the CTEStore`; a `SegmentStore` round-trip
  (append → render) lands through it.

---

## Thread A — Expose the live context plane over the gact REST API / TUI stream

All edits are in the worktree. Target files: `src/clio_agent/gact/app.py` (routes + the
streaming wiring fix) and `src/clio_agent/gact/types.py` (response/request models). Routes are
declared **inside the `build_app(sessions_path, agent, arc) -> FastAPI` factory**
(def at `app.py:12603`, `FastAPI()` at `app.py:12627`) as nested coroutines with decorators
indented **4 spaces**, placed after the `app.state.*` block (which runs `app.py:12658-12710`).

Standing rules every new route obeys (verified shapes):
- **404 on missing session:** `if app.state.sessions.get(sid) is None:` →
  `raise HTTPException(404, detail=ErrorEnvelope(error=ErrorInfo(error="not_found",
  message=..., details={"session_id": sid}, recoverable=True)).model_dump(exclude_none=True))`
  (exact pattern at `app.py:13711-13721`).
- **503 when memory disabled:** `if app.state.arc is None:` →
  `raise HTTPException(503, detail=ErrorEnvelope(error=ErrorInfo(error="arc_unavailable",
  ...)).model_dump(exclude_none=True))` (mirror the `arc is None` style at `app.py:12940`).
- Imports already in scope: `FastAPI, HTTPException, Request` (`app.py:155`),
  `JSONResponse, Response, StreamingResponse` (`app.py:158`),
  `Event, EventBus, heartbeat_payload` (`app.py:12301`). New Pydantic models go in
  `types.py` (next to `SessionContextPolicy` at `types.py:203`); reuse `SegmentKind` from
  `clio_agent.arc.schema` (`schema.py:61`).

Live-plane methods on `ARCMemory` (all callable as `app.state.arc.<m>`):
`render_segments(session_id, scope, *, as_of=None) -> list[Segment]` (`memory.py:1120`),
`render_segments_keys(...) -> dict` (`memory.py:1124`),
`render_segment_text(...) -> str` (`memory.py:1131`),
`segment_tokens_by_kind(session_id, scope) -> dict[str,int]` (`memory.py:1135`),
`apply_segment_op(op, session_id, scope, **kwargs)` (`memory.py:1116`).
`Segment` is a `msgspec.Struct` (`arc/schema.py:67`) — serialize with `msgspec.to_builtins(seg)`
(idiom at `app.py:418`).

**Insertion point for the two new routes:** immediately after the
`GET /v1/sessions/{sid}/context/policy` handler ends at **`app.py:13744`** (before
`@app.delete("/v1/sessions/{sid}")` at `app.py:13746`) — keeps `/context/*` routes together.

### A.(a) GET context state — `GET /v1/sessions/{sid}/context/state`

Insertion point: after `app.py:13744`. Pure read/derivation (no decision).

```python
    @app.get("/v1/sessions/{sid}/context/state", response_model=ContextStateResponse)
    async def get_context_state(
        sid: str, scope: str, as_of: int | None = None
    ) -> ContextStateResponse:
        ...
```

`scope` is a **required** query param (the live plane is always scope-addressed — `ARCMemory`
deliberately does not publicly enumerate scopes; only `SegmentStore.scan_scopes` does, at
`segments.py:501`). `as_of: int | None` is optional (passed through to `render_*`). Body:
1. 404 if `app.state.sessions.get(sid) is None`; 503 if `app.state.arc is None`.
2. `tokens_by_kind = arc.segment_tokens_by_kind(sid, scope)`
3. `segments = arc.render_segments(sid, scope, as_of=as_of)`; `live_block_count = len(segments)`
4. `live_tokens = sum(tokens_by_kind.values())`
5. **window (denominator):** `window = _resolve_expert_context_window(getattr(app.state.agent,
   "_provider_config", None))` — `_resolve_expert_context_window` (`app.py:5547`) needs an
   *attribute-bearing object*; `agent._provider_config` is the live `LMProviderConfig`
   (set at `agent.py:212`, confirmed). Guard `agent is None` ⇒ `window = 0`. (Do NOT pass
   `_effective_lm_config(app)` — it returns a dict, not an attr object.)
6. **numerator:** the deterministic offline numerator is `live_tokens` (segment-store
   attribution). The provider-exact `_last_prompt_tokens()` (`app.py:5501`) only has meaning
   *inside an active turn* (reads turn-local `dspy.settings`) — **not reliable from a cold REST
   call**, so do not use it here. Document that `pct_used` here is the segment attribution,
   distinct from the in-turn provider reading the autocompactor uses.
7. `pct_used = (live_tokens / window) if window else None` (window 0/unknown ⇒ `null`, mirroring
   the "auto-compaction disabled" semantics at `app.py:236-237`).
8. `render_text = arc.render_segment_text(sid, scope, as_of=as_of)` (human block);
   `render_keys = arc.render_segments_keys(sid, scope, as_of=as_of)` (trajectory dict).
9. `segments=[msgspec.to_builtins(s) for s in segments]`.

`ContextStateResponse` (add to `types.py` near line 203): `session_id: str`, `scope: str`,
`as_of: int | None`, `window_tokens: int`, `live_tokens: int`, `pct_used: float | None`,
`live_block_count: int`, `tokens_by_kind: dict[str, int]`, `segments: list[dict]`,
`render_text: str`, `render_keys: dict`. (Drop the optional `tokens_by_scope` unless the
scope-enumeration passthrough below is added.)

### A.(b) POST a context op — `POST /v1/sessions/{sid}/context/ops`

Insertion point: immediately after (a), still before `app.py:13746`. Thin validated
passthrough to the sanctioned dispatch seam `apply_segment_op` (no decision; clio does not
choose the op — the caller does).

```python
    @app.post("/v1/sessions/{sid}/context/ops", response_model=ContextOpResponse)
    async def post_context_op(sid: str, req: ContextOpRequest) -> ContextOpResponse:
        ...
```

`ContextOpRequest` (in `types.py`): `op: Literal["append","insert","delete","summarize"]`,
`scope: str`, and op-specific optionals `kind: SegmentKind | None`, `content: dict | None`,
`position: int | None`, `ids: list[str] | None`, `summary_content: dict | None`,
`step: int = -1`, `token_count: int = 0`, `trace_ref: str = ""`.

Per-op required kwargs (from `segments.py`, fed to `apply_segment_op`):
- `append`: `kind, content` (+ optional `step, trace_ref, derived_from, token_count`) — `segments.py:176`
- `insert`: `position, kind, content` (+ same optionals) — `segments.py:216`
- `delete`: `ids: list[str]` → returns count tombstoned — `segments.py:271`
- `summarize`: `ids, summary_content` (+ `trace_ref, token_count`) → returns new `Segment` — `segments.py:298`

Body:
1. 404 (missing session) / 503 (`arc is None`).
2. Build a `kwargs` dict containing **only** the fields relevant to `req.op` (append ⇒
   `{kind, content, step, token_count, trace_ref}`; insert adds `position`; delete ⇒ `{ids}`;
   summarize ⇒ `{ids, summary_content, token_count, trace_ref}`).
3. `result = app.state.arc.apply_segment_op(req.op, sid, req.scope, **kwargs)`.
4. **Wrap `ValueError` (bad op — `apply` raises `ValueError(f"unknown segment op: {op!r}")` at
   `segments.py:383`) and `TypeError` (missing/extra kwargs) into a 400**
   `ErrorEnvelope(error="invalid_request", ...)`.
5. The op **auto-emits `arc.op`** via the wired `op_logger` (`SegmentStore._finish_write`,
   `segments.py:409-425`; logger wired at `app.py:12705-12710`) — **no manual event publish in
   this route.** Once (c) lands it also reaches the SSE stream.

`ContextOpResponse`: `session_id, scope, op, applied: bool`, plus `result` —
`msgspec.to_builtins(segment)` for append/insert/summarize, `tombstoned_count: int` for delete.
Also return a fresh `tokens_by_kind` / `live_block_count` / `pct_used` snapshot so the TUI
updates without a second GET.

### A.(c) Make `arc.op` reach the TUI SSE stream

No new route — wiring/redaction fix only. The op-logger already fires on every applied op
(`segments.py:409`, wired `app.py:12705-12710`) and `_emit_arc_op` (`app.py:467`) emits the
event. **The gap:** `_emit_arc_op` calls `_emit_semantic_event(..., detail_level="off")`
(confirmed at `app.py:517`), and in `SemanticEventSink.emit` the bus publish is gated by
`if event.detail_level != "off":` (`semantic_events.py:471`). So the durable trace + ARC
live-fold get the op (`semantic_events.py:461-468`, ungated) but `bus.publish`
(`semantic_events.py:472`) is **skipped** — `arc.op` never reaches
`GET /v1/sessions/{sid}/events` (`app.py:20926`) and thus never reaches the TUI.

**Chosen fix — Option B (explicit typed bus event), recommended:** inside `_emit_arc_op`
(`app.py:467`, before/after the `return _emit_semantic_event(...)` at `app.py:492`), add an
explicit publish of a **redacted** `arc.op` event:

```python
    app.state.bus.publish(Event(
        type="arc.op",
        session_id=session_id,
        payload={
            "op": op, "scope": scope, "logical_time": logical_time,
            "step": step, "position": position,
            "segments_written": [
                {"id": s.get("id"), "kind": s.get("kind"),
                 "token_count": s.get("token_count")}
                for s in (segments_written or [])      # ids + kinds + token_count ONLY
            ],
            "segments_tombstoned": segments_tombstoned or [],
        },
    ))
```

`Event` is already imported (`app.py:12301`). This keeps the **durable trace lean**
(`detail_level="off"` unchanged — op events are high-volume, `app.py:486-487`) while giving the
TUI a **first-class `event: arc.op` SSE frame** that is already redacted by construction
(omit `content`/`args`/`text`, which `_emit_arc_op` flags SENSITIVE at `app.py:485`). The SSE
route itself is unchanged — `EventBus.subscribe` / `_format_sse` (`app.py:297`, `app.py:20996`)
fan it out per session.

**Rejected — Option A (1-line):** flip `detail_level="off"`→`None` at `app.py:517` so `arc.op`
rides the existing `semantic.event` publish. Downside: re-enables durable+SSE for a
high-volume event and bloats the `semantic.event` channel; the TUI must then dispatch on
`payload.event_type == "arc.op"` instead of a clean `event:` line. Use only if Option B's
extra publish proves problematic.

**TUI note (return to caller):** with Option B the TUI keeps its single subscription to
`GET /v1/sessions/{sid}/events` and adds a handler for `event: arc.op` (payload = the redacted
dict above). No new subscription mechanism. The state panel re-fetches `GET .../context/state`
(or applies the `ContextOpResponse` snapshot from its own POST) on each `arc.op` frame.

### A — optional follow-up (only if the TUI needs a scope picker)

`ARCMemory` does not publicly enumerate scopes. If a per-session scope picker is needed, add a
one-line passthrough on `ARCMemory` (`memory.py`): `def scan_segment_scopes(self, session_id,
prefix=""): return self._segments.scan_scopes(session_id, prefix)` (underlying
`SegmentStore.scan_scopes` at `segments.py:501`), then add a `tokens_by_scope` field to
`ContextStateResponse` and a `GET .../context/scopes` route. **Not in this thread** — the live
plane is scope-addressed, so the GET/POST routes take `scope` explicitly.

### A — files touched

- `src/clio_agent/gact/app.py` — routes (a) and (b) after **`app.py:13744`**; add the
  redacted `bus.publish` in `_emit_arc_op` at **~`app.py:492`** for (c).
- `src/clio_agent/gact/types.py` — `ContextStateResponse`, `ContextOpRequest`,
  `ContextOpResponse` near `SessionContextPolicy` (**`types.py:203`**); reuse `SegmentKind`
  from `clio_agent.arc.schema` (`schema.py:61`).

### A — tests

- (a) `GET .../context/state`: TestClient over `build_app(...)` with an `ARCMemory` that has a
  few appended segments; assert `live_block_count`, `tokens_by_kind`, `pct_used` math, and
  `null` `pct_used` when window unknown (agent/`_provider_config` absent). 404 unknown session,
  503 `arc=None`.
- (b) `POST .../context/ops`: each op round-trips and is reflected by a follow-up GET; bad `op`
  ⇒ 400; missing required kwarg (e.g. `append` without `content`) ⇒ 400 (TypeError wrap).
- (c) Subscribe to the SSE route, apply a context op, assert an `event: arc.op` frame arrives
  with a redacted payload (no `content`/`args`/`text`; has `op/scope/logical_time` and
  `segments_written` id/kind/token_count only).

No deterministic decision-making is introduced anywhere in A: (a) is a pure read/derivation,
(b) is a validated passthrough to the sanctioned `apply_segment_op` dispatch seam, and (c) is a
transport/redaction wiring fix — all "surface reality, don't decide".

---

## Dependency order (dependencies, not phases)

These are ordering constraints, not a milestone sequence. Anything not linked can proceed in
parallel.

1. **B is independent of A.** The CTE store changes nothing in the `ARCMemory` /
   `SegmentStore` API surface that A consumes — A's live-plane methods
   (`render_segments` / `apply_segment_op` / `segment_tokens_by_kind`) are store-agnostic and
   already exist on `ARCMemory`. A can be written and tested against the LocalFS default with
   no CTE present.
2. **Within B:** `CTEStore` (B.2) must exist before `make_arc_store` (B.3) can reference it;
   `make_arc_store` must exist before the `agent.py:202` wiring (B.4). The `IOWarpCTEBackend`
   deletion can happen any time (dead code, no dependents).
3. **Within A:** the two response/request models in `types.py` must exist before the routes in
   `app.py` import them. Routes (a) and (b) are mutually independent. The streaming fix (c) is
   independent of both routes — it edits `_emit_arc_op`, which (b) only triggers indirectly.
4. **Cross-thread coupling is at the seam only:** A's POST op (b) → `apply_segment_op` →
   `SegmentStore` → `self._store` (whatever B selected). So the **only** place B and A meet is
   that A's writes/reads land in whichever store `make_arc_store` chose. A's correctness does
   not depend on which store; B's correctness does not depend on the routes. Run A's tests
   under both `CLIO_ARC_STORE=local` and `=cte` as the **integration cross-check** (the single
   merge-time gate), but neither thread blocks the other's development.

Critical-path summary: `CTEStore → make_arc_store → agent.py wiring` (B) and
`types.py models → routes (a)/(b)` + `_emit_arc_op fix (c)` (A) are two parallel chains that
join only at the `make_arc_store` selection consumed by A's ops at runtime.

---

## Key risks

1. **CTE binary-decode trap (B, high-impact, mitigated).** `Tag.GetBlob` UTF-8-decodes in the
   C++ binding and raises on non-UTF-8 bytes; ARC stores arbitrary msgpack. The base64 wrap is
   the fix, but it is **silent if a future code path bypasses it** — any direct `PutBlob`
   without `b64encode` will corrupt on read. Mitigation: confine all blob I/O to `CTEStore`
   methods, and keep the `b"\x00\x83\xff\x81"` round-trip regression test.
2. **DRAM-only = no disk durability (B, product-level).** The default CTE config is a single
   in-memory tier; data evaporates when the process exits. Anything ARC is expected to survive
   across restarts (conversations, profiles, procedural memory) will silently vanish under
   `CLIO_ARC_STORE=cte` until a `file` bdev + `is_restart=True` WAL replay is configured.
   Decide explicitly whether CTE should be the **default** before a durable tier exists — this
   plan defaults to `cte` per the grounding, but `local` is the safe default if durability is
   required now. This is a one-line flip in `make_arc_store`.
3. **In-process runtime lifecycle (B).** `chimaera_init` is once-per-process and undefined on a
   second call; it boots a co-process that dies with the interpreter and leaves shm/symlink
   residue (auto-reaped next init). Risks: a test suite that constructs many `ARCMemory(...)`
   in one process (guard handles it), a forking server worker model (each fork would need its
   own init — uvicorn default single-process is fine; `--workers >1` is **not** validated),
   and orphaned `/dev/shm` under hard crashes. Mitigation: keep the class-level init guard,
   document single-worker, and add a doctor probe that reaps stale `/tmp/chimaera_*`.
4. **`_resolve_expert_context_window` needs an attr object, not a dict (A).** Passing
   `_effective_lm_config(app)` (a dict) silently yields `0` → `pct_used=null` everywhere. The
   plan pins `getattr(app.state.agent, "_provider_config", None)`; if the agent is rewired to
   stop carrying `_provider_config` (it is set at `agent.py:212`), the percentage goes dark.
   Low-severity (degrades to `null`, never crashes) but worth a test that asserts a non-null
   `pct_used` when an agent is present.
5. **`arc.op` redaction completeness (A).** Option B hand-builds the streamed payload; if a
   future op adds a sensitive field to `segments_written`, the explicit allow-list
   (`id`/`kind`/`token_count` only) protects it — but a careless edit that spreads the full
   segment dict into the payload would leak `content`. Mitigation: keep the allow-list explicit
   (never `**s`) and assert redaction in the SSE test.
6. **Cold-vs-turn numerator semantics (A, correctness-of-meaning).** `pct_used` from the REST
   route is the **segment-store attribution**, not the provider-exact prompt-token count the
   in-turn autocompactor uses; the two can diverge. Risk is user confusion, not a bug —
   mitigate by documenting the distinction in the `ContextStateResponse` field doc.
7. **Dead-code removal blast radius (B, low).** Deleting `IOWarpCTEBackend` is safe per grep
   (only self-references), but confirm no test imports it before removing; if any do, delete or
   re-point them in the same change.
