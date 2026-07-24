# Codex-net: egress recording + read-allow/write-gate under the codex fence (2026-07)

## Why (de-deferral)

The codex write-fence rework (`docs/design/codex-sandbox-rework-2026-07.md`) shipped the OS
write-confinement pillar of #974 but **deferred** the network pillar: srt's chokepoint was
wired to srt's proxy plumbing, and codex networks differently, so `_activate_codex` stamped
`net_enforcement="codex-net-deferred"` (honest, typed) and `wrap_confined` gated the per-child
egress wiring to non-codex mechanisms.

Owner directive (2026-07-23): **de-defer it — implement properly.** "Part of observability and
sandboxing has no value" without egress recording. The defer was only justified while proving
codex could replace srt at all; that is proven, so the net pillar now gets done right, on both
platforms, before #974 sign-off.

## Network policy (owner, load-bearing)

- **Reading/ingesting from the internet is ALLOWED BY DEFAULT, but RECORDED.** Gating inbound
  reads behind a human approval is the fastest way to get users to rip the sandbox out. Every
  read egress is observed and lands on the durable trace as a `net.egress` record.
- **Writing to the internet is blockable + escalated for HUMAN PERMISSION.** A write-shaped
  egress may be denied pending an approval event.
- **Honesty caveat (no overclaiming):** an HTTPS `CONNECT` tunnel is opaque — clio sees only
  `host:port`, not the method/body. So write-detection is clean only for **plain-HTTP verbs**
  (`POST`/`PUT`/`PATCH`/`DELETE`) and **host policy**. The default posture is **record-all**;
  the write-gate applies where the request shape is observable. This limit is documented, never
  papered over.

## The model — Recipe A (PROVEN LIVE on Windows 2026-07-23)

Codex ships its own managed egress proxy (`codex-network-proxy`, since v0.118) that **forces all
sandboxed-child traffic through it** when a network profile is enabled, and **chains to an
upstream** proxy via `allow_upstream_proxy = true` (default). So:

```
fenced child ──▶ codex managed proxy (codex injects it into the child's env)
             ──▶ UPSTREAM = clio chokepoint  (loopback, runs OUTSIDE the child fence)
             ──▶ internet
```

The hop to clio's loopback is made by **codex's proxy (parent-side, unfenced)**, not by the
fenced child. That is precisely why the earlier B-codex-2 attempt HUNG (it pointed the *child* at
clio's loopback, which the fence blocks) and why this works.

### Proven recipe (Windows, `out/codex-sandbox/codex_net_recipe_a_probe.py`)

`-p` layer profile gains a network table (alongside the filesystem grants):

```toml
[permissions.clio.network]
enabled = true
mode = "full"              # observe-all, block-nothing at codex's layer = reads allowed by default
allow_upstream_proxy = true
# NOTE: `mitm` is a TOML TABLE in v0.145 (not a bool) — OMIT it; default = off = plain CONNECT
#       tunneling, so clio needs no CA. clio MITMs at its OWN layer only if it ever wants payloads.
```

Set the proxy env on the **codex parent process** (never the child): `HTTP_PROXY` / `HTTPS_PROXY`
/ `ALL_PROXY` (+ lowercase) = `http://127.0.0.1:<clio-listener-port>`, `NO_PROXY=""`.

Live result: clio's upstream proxy recorded `CONNECT example.com:443` (HTTPS, opaque host:port)
and `GET http://example.com/` (HTTP, **verb visible**), child `HTTP 200` rc=0 end-to-end, with
**no codex-log parsing**.

### Live-caught caveats (in the probe)

1. `mitm` must be omitted (table, not bool) — else `invalid type: boolean, expected struct`.
2. Don't launch the child as uv's python shim (unreadable to the restricted `codexsandbox*`
   user → rc103 "No Python"). The real fleet children are python/httpx and are fine.
3. Windows `curl.exe` fails TLS as the restricted user (schannel `SEC_E_NO_CREDENTIALS`) — a
   curl-under-restricted-token artifact, **not** a recording failure. python/httpx uses certifi.

## Design — reuse the ladder seams

The integration points already exist from the srt era; the mechanism underneath changes.

- **`sandbox_codex.synthesize_codex_profile`**: add the `[permissions.<p>.network]` table
  (`enabled=true`, `mode="full"`, `allow_upstream_proxy=true`; omit `mitm`). clio-side validated
  (closed key set) like the filesystem synth — a drifted profile is a typed
  `codex_profile_rejected`, never a silent no-op.
- **`wrap_confined` (`sandbox.py`)**: un-gate the per-child egress wiring for `MECHANISM_CODEX`.
  `sandbox_net.open_child_egress` returns `(net_child_id, proxy_port, net_env)`; the `net_env`
  proxy overlay lands on the spawned process — which for codex **is the codex parent**, exactly
  what Recipe A requires. **Per-child attribution is PRESERVED**: each `wrap_confined` spawns one
  codex process for one confined child and gives it its own clio listener port, so egress
  arriving on that port is keyed to that child_id (codex's proxy is the immediate client, but the
  port identifies the child).
- **`_activate_codex`**: flip `net_enforcement` `codex-net-deferred` → `proxy-enforced` (the child
  cannot bypass — codex's OS egress rules force it through the managed proxy → clio upstream).
- **Write-gate**: clio's existing `set_egress_gate` (B5 deny-mode CONNECT gate) classifies
  write-shaped egress (plain-HTTP verb / host policy) and returns deny-pending-approval, minting a
  permission event; HTTPS CONNECT is recorded but allowed by default (opaque). Reads always
  allowed + recorded.

## Slices

- **N1 — recording**: profile network table + `wrap_confined` codex net wiring + `net_enforcement`
  flip + unit/selection tests. Deliverable: a confined codex child's egress is recorded through
  clio's upstream, per-child attributed. Live gate: Windows (proven) + Linux.
- **N2 — read-allow/write-gate policy**: the egress classifier + permission event + honest
  CONNECT-opacity handling; deny path fail-CLOSED (per B5 lesson) for writes only, reads never
  blocked. Unit + live deny gate.
- **N3 — cross-platform live gate + attribution proof**: real fleet stage→clean turn under the
  codex fence records `net.egress` with real domains on both platforms; per-child attribution
  verified; `used web:<domain>` lineage joins (composing with the step-0 tool-declared floor,
  already landed at b5b6c7c0). This closes #1024's observed-domain path too.

## Live gates (the acceptance)

Reuse `out/codex-sandbox/codex_net_recipe_a_probe.py` (the primitive). Full gate: a real
earthscope stage→clean turn under the codex fence records `net.egress` with real domains
(`nationaldataplatform.org`, …), per-child attributed; a write-shaped egress is gated to a
permission ask; zero untyped degrades. Windows (elevated, one UAC already done) + Linux (WSL
node22/codex, bwrap — watch the DNS caveat codex#22387).

## Deletion / change inventory (lands with the replacement)

- `_activate_codex`'s `codex-net-deferred` value RETIRED (→ `proxy-enforced`); its docstring
  "needs NO chokepoint proxy" line removed.
- The `wrap_confined` `!= MECHANISM_CODEX` net-gate REMOVED (codex now wires net like the other
  active backends).
- `codex-net-deferred` references in `sandbox_doctor.py` / tests updated to the active reason.
- No new store, no god-file accretion (owner modules: `sandbox_codex.py`, `sandbox_net.py`,
  `net_chokepoint.py`, `ingest_edges.py`).

## Fleet under codex (LIVE-VALIDATED 2026-07-24)

The `codex_net_recipe_a_probe.py` primitive proves egress recording with a curl child, but curl
works only because `System32` is world-exec. Running the **real released web MCP server**
(python/httpx, fastmcp, clio-kit 2.6.3) confined under the codex fence revealed a cascade of
fleet-under-codex requirements curl could not. The demo (`out/codex-sandbox/fleet_demo.py`) now
passes — web server started confined, `web.fetch(http://example.com)` → status 200, and clio's
chokepoint recorded the httpx egress, per-child attributed — but only after the three fixes below,
which are now **automatic** (no manual setup).

### A. Confined-child cache-env redirect (cross-platform — the most important)

A real python/fastmcp MCP server writes a per-user **profile cache at import time**; under the
read-only OS fence that default location is out-of-territory, so the server crashes
`PermissionError` before it can serve. `wrap_confined` (`runtime/sandbox.py`) therefore, when a
fence is **active and `write_roots` is non-empty**, points the child's cache/temp env at a
per-child cache dir `<write_roots[0]>/.clio-child-cache` (created best-effort, typed log on
failure, never breaks the spawn). The vars set (`sandbox._child_cache_env`):

- `LOCALAPPDATA`, `APPDATA`, `XDG_CACHE_HOME`, `TEMP`, `TMP`, `FASTMCP_HOME` → the per-child dir
- `FASTMCP_CHECK_FOR_UPDATES=off` (belt-and-suspenders — no update check → no cache write)

`FASTMCP_HOME` is load-bearing and separate: fastmcp writes `version_cache.json` to `settings.home`
via platformdirs' **Windows API**, which `LOCALAPPDATA` does **not** redirect — only `FASTMCP_HOME`
does (proven live). The redirect points at the child's ALREADY-writable workspace — it is **not** a
new grant and does **not** widen the write fence. On the **floor** (inactive) NONE of these are set
— the floor `env_overlay` stays byte-identical. Linux (bwrap, same-user) needs this too.

### B. Windows `codexsandbox*` RX grants on the fleet runtime (Windows-only)

After codex's setup helper creates the dedicated `CodexSandboxOffline` / `CodexSandboxOnline`
accounts, `CreateProcessAsUserW` still fails **`WinError 5`** unless those restricted users can
read+exec the fleet runtime they must launch (the per-user profile tree denies them by default).
`clio sandbox setup` now runs `grant_fleet_runtime_access` (`runtime/sandbox_cli.py`) as part of
provisioning — an own-profile DACL edit (the current user owns these paths → no admin needed) that
`icacls /grant`s **both** users, per path, emitting a structured reason each (`granted` /
`skipped_missing` / `failed`), never failing setup:

- `(OI)(CI)(RX)` `/T` on: the **uv tool bin dir** (the clio-kit launcher — resolved from the
  launcher on PATH, else `~/.local/bin`), `~/AppData/Roaming/uv/tools`,
  `~/AppData/Roaming/uv/python` (the uv-managed python trampoline target), and `~/.cache/clio-kit`
- `(RX)` traverse (no `/T`) on `~/AppData/Local/Temp` — the confined workspace lives under it

A path that does not exist is a typed skip (never a failure). The per-grant reasons land on
`CodexProvisionResult.extra['fleet_runtime_grants']`.

### C. MCP-server envs must be PRE-PROVISIONED

A confined child **cannot build its uv venv** (write-fenced), and the clio-kit launcher's on-demand
env install/verify FAILS under the fence (`web-mcp not found … install the server package`). So the
fleet must **pre-build the MCP-server envs** before the confined spawn, or spawn the pre-built env
python **directly** (`<env>/Scripts/python.exe -m web_mcp.server`, bypassing launcher-managed
install) rather than routing through the launcher's on-demand management. Linux (bwrap, same-user)
skips the RX grants (B) but still needs both the cache redirect (A) and pre-provisioned envs (C).
