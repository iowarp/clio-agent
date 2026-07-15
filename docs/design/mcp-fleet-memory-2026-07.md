# MCP Fleet Memory: O(1) Servers for N Sessions on an 8 GB Desktop

Status: LANDED S1–S5 (2026-07-15). Grounds issue #929; umbrella #930.

| Slice | Issue | PR | Landed result (accepted 3-session haiku gate, real CTE) |
| --- | --- | --- | --- |
| S1 measurement + budget gate | #931 | #937 | `scripts/mcp_mem_attribution.py` + ratchet file; baseline 3.57 GB peak==final |
| S2 spawn-once + lazy per-namespace | #932 | #938 | idle 0.35 GB; only CALLED namespaces spawn; typed unknown-name guard (fastmcp ≥3.4 composite pin) |
| S3 idle-TTL + LRU reaper | #933 | #939 | final 0.67 GB after settle, ALL fleets reclaimed via typed `workspace_fleet_reaped`; drain guard + turn leases |
| S4 spawn diet | #934 | #940 | called fleet 2 procs / 0.11 GB (was 6 + launcher + resident uv.exe); learned plans, TTL-bounded 24h; upstream resolve-probe ask iowarp/clio-kit#296 |
| S5 ratchet + enforce | #935 | close-out PR on this doc | budget 3.57/3.57 → 1.42/0.72 (peak = honest cold-boot maximum, finals = median of 0.72/0.74/0.72); CI test pins recorded ≤ campaign targets; release-skill gate wired; fleet-lifecycle docs |

Measured trajectory: 3.57 GB peak==final → 1.42 GB cold peak / 1.16–1.28 GB
warm peak (the transient boot listing pass) / 0.72 GB settled. Both campaign
targets (≤1.8 peak, ≤1.3 post-idle) met with margin.
Date: 2026-07-14

## 0. Goal

**N concurrent sessions cost an O(1) resident MCP fleet, not O(N·blueprints),
with cwd/artifact semantics preserved — validated on an 8 GB desktop profile
and locked by a release-gating memory budget on the standard acceptance load**
(3 concurrent claude-haiku sessions, real CTE). Measured baseline today:
**3.57 GB process tree at peak == final** (nothing reclaimed); target after
this campaign: **≤ 1.8 GB peak, ≤ 1.3 GB after sessions idle**, ratcheting
down once recorded.

## 1. The measured problem (#929)

On the #921 acceptance load: MCP servers 2.15 GB across ~72 processes,
`uv.exe` launcher layers 0.56 GB (12 × ~47 MB), conhosts 0.10 GB — versus a
small product core (gact server 0.33 GB, pooled claude CLI 0.37 GB, CTE
daemon 0.05 GB: the bounded-arena work held).

## 2. Lifecycle facts (code-verified)

1. **Union mounting**: `agent._discover_pack_servers` merges `mcp_servers`
   from EVERY discovered blueprint; session activation changes nothing about
   which servers mount. Inactive blueprints' servers (geo/ndp) spawn anyway.
2. **Lazy proxies, defeated**: `build_gateway` constructs lazy proxies (no
   spawn), but `SyncMCPToolExecutor.start()` runs `list_tools` against the
   composite gateway — an eager full-fleet fan-out spawn.
3. **Boot double-spawn (#702 still real)**: catalog derivation opens+closes
   the whole fleet once, then the live executor spawns it again.
4. **Per-workspace fleets, never reclaimed**: `_workspace_tool_executors` is
   an unbounded dict keyed by workspace root; entries die only at process
   shutdown. No TTL, no LRU, no session/workspace-close hook.
5. **Launcher overhead**: the resident `uv.exe`/`clio-kit` layer is the
   declared launcher command itself (resolved via `shutil.which`), kept alive
   as the parent of each server. conhost.exe rides every child (SDK spawns
   CREATE_NO_WINDOW; the headless console still costs one conhost each).

## 3. Load-bearing semantics (MUST survive)

- **cwd pinning + `CLIO_KIT_ARTIFACTS`**: relative/default-output tools
  (plot, export) write into the bound workspace only because the server
  process cwd/env say so (`transport_for`; `_ground_output_paths` repairs).
- **Per-workspace artifact isolation**: two sessions on different workspaces
  must never write into each other's roots.
- **`UV_CACHE_DIR` isolation** (the uvx cold-cache corruption race).
- **#900 reaping invariant**: no orphaned children on hard kill.

Explicitly NOT semantics (safe to redesign): fleet-per-workspace *residency*,
the boot double-spawn, eager start fan-out, forever-resident executors.

## 4. Slices

### S1 — Measurement owner + budget baseline
Promote the gate sampler to `scripts/mcp_mem_attribution.py` (per-process
tree attribution, role-classified, peak/final report). Record the current
numbers as the budget baseline in a ratchet file; add the live-gate assertion
(the 3-session load must stay under the recorded budget; budget only
ratchets DOWN). Also diagnose the observed 4-fleets-on-one-workspace anomaly
(is the catalog-derivation fleet actually reaped on Windows, or do those
children survive client close?).

### S2 — Spawn once, spawn lazily, per-namespace
Kill the boot double-spawn (derive the catalog from the SAME connection the
live executor keeps, or from cached tool metadata). Make executor `start()`
stop fanning out: connect a namespace's proxy on FIRST CALL to one of its
tools (per-namespace lazy connect), so mounted-but-unused servers (inactive
blueprints' geo/ndp) cost zero processes. This preserves union mounting
semantics while making the union free until used.

### S3 — Reclamation: TTL + LRU on workspace fleets
Idle-TTL reaper (default ~120 s after last tool call) + LRU cap on
`_workspace_tool_executors`, drain-aware (in-flight refcount; never reap
mid-call; the #867 drain-lock pattern) with a typed trace reason per reap.
Hooks: workspace close and session idle also release. Steady state becomes
O(active workspaces), reclaimed promptly — all cwd semantics intact because
fleets are still per-workspace while alive.

### S4 — Spawn diet
Resolve stable launchers (clio-kit) to the DIRECT venv interpreter command
once per server (drop the resident `uv.exe` parent layer, ~47 MB × N),
keeping `UV_CACHE_DIR` isolation for genuinely uvx-launched servers.
Verify conhost count falls with process count; confirm the SDK job-object +
#900 reaping still cover direct-spawned children.

### S5 — Re-validate + enforce
Re-run the 3-session haiku acceptance load; record the new budget (expected
≈1.6 GB peak / ≈1.2 GB idle); wire the budget gate into the release checks
(bounded memory is release-gating); docs pass (ENVIRONMENT/architecture note
on fleet lifecycle + the budget contract). Campaign-done gate: budget held
live, zero typed degrades, all semantics tests green.

### Stretch (own decision gate, only if S2+S3 measurements demand it)
Shared-process fleets for absolute-path-only namespaces (hdf5/parquet/
pandas) with per-call path grounding enforced at the executor boundary.
NOT attempted while `CLIO_KIT_ARTIFACTS`-staging namespaces (plot/geo/ndp)
would mix workspaces in one process — that line is the correctness boundary
the owner set: optimize without changing semantics.

## 5. Budget math (why this reaches 8 GB desktops)

Post S2–S4 steady state: active-namespace fleet spawned once (~0.45 GB for
four data servers without launcher layers) + one live workspace fleet during
a turn (reaped after TTL) + server 0.33 + claude CLI 0.37 + CTE 0.05
≈ **1.2–1.6 GB total** — inside an 8 GB machine's ~3–4 GB free envelope with
headroom, versus 3.57 GB-and-growing today.
