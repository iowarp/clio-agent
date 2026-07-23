# Sandboxing campaign (2026-07) — the landed record

**Umbrella:** [#974](https://github.com/iowarp/clio-agent/issues/974) · **Authority:**
`docs/design/artifact-provenance-design.md` §5 (Layer 3). **Precondition:** #966 (artifacts)
landed — the registry, transform records with `mechanism` + per-edge evidence, and
gap/contended nodes exist; Campaign B upgrades their *guarantees*.

This is Campaign B of the artifacts/sandboxing pair: OS-level write confinement for every
process the agent spawns, a network chokepoint that records all child egress, grants as
first-class recorded events, and the provenance-tier UPGRADES that turn #966's
honest-but-unenforced floor into enforced guarantees. It invents no new provenance
vocabulary — it makes #966's concepts TRUE: an unwatched out-of-root write becomes a typed
`policy_violation` instead of a `gap`; correlated `lease-window` evidence becomes
`fence_proven`; unobserved egress becomes a recorded `used web:<domain>@<time>` ingest edge.

Campaign discipline (per slice): tests + sabotage suites → adversarial subagent review → PR
→ live gate on the accepted substrate, **floor-first** and per-platform so nothing is ever
silently wrong (#966 labels every degradation permanently).

---

## The slices (all landed)

| Slice | Issue | What landed |
|---|---|---|
| B1 | #975 | `runtime/sandbox.py` owner module (the backend ladder + `wrap_confined` composition point) on the typed floor; the three spawn seams routed through it (zero behavioral change); doctor row + census `confinement` column + boot `sandbox.state` event. |
| B2 | #976 | srt + native Landlock fences ACTIVATED on the typed degrade ladder (Linux bwrap+proxy / macOS Seatbelt); `gap → policy_violation` mint (EROFS/EACCES → `prevented`/`detected`); the full degrade ladder on one OS. |
| B3 | #977 | Windows: `clio sandbox setup` (one-time self-elevating UAC) provisions the srt principal + WFP fence; the `srt_windows` rung + `WinError 5` → `policy_violation`; shell-reuses-fleet (Windows fs policy is session-wide). |
| B4 | #978 | Network chokepoint: one clio-owned CONNECT proxy, per-child egress channels, `net.egress` trace-only events, and the `used web:<domain>@<time>` ingest join (child-keyed, precision over recall). |
| B5 | #979 | Grants on the record: `boundary.*` semantic events, a mid-session root-grant API, a `host_pattern` domain vocabulary, opt-in deny-by-default mode (grant-on-first-domain), all reusing the existing permission gate + policy store. |
| B6 | #980 | **Guarantee closure** (this slice): the `lease-window → fence_proven` per-edge upgrade, fleet territory-enforcement closure, and the `sandbox_conformance` sweep + campaign-done gate suite. |

---

## Owner-locked design decisions (#974)

1. **Backend ladder: srt → Landlock → none; srt IS the Windows path.** `runtime/sandbox.py`
   is the twin of `runtime/process_tree.py`. srt = `@anthropic-experimental/sandbox-runtime`
   (library mode: macOS Seatbelt, Linux bwrap+proxy, Windows ACL/WFP) → native Landlock
   (Linux fs-fence, the answer to bwrap broken on Ubuntu 24.04+ AppArmor userns restriction)
   → `none` (typed floor). **Not built:** a native Windows restricted-token/ACL/WFP
   implementation (srt is the Windows path; a documented future ladder rung).
2. **Windows bar** = one-time self-elevating idempotent `clio sandbox setup` (single UAC);
   per-session use is unprivileged; every Windows record is labeled write-fence-grade.
3. **Network default = ALLOW + RECORD** through one CONNECT-only proxy
   (`runtime/net_chokepoint.py`). Deny-by-default + grant-on-first-domain is opt-in per
   workspace. CONNECT-only = domain-level records, no MITM/CA breakage.
4. **Host-OS confinement only** — no container substrate (docker distribution ships
   separately).
5. **Wrap composition, not a second wrapper.** `wrap_confined` is the single argv-prefix
   owner; the fence prefix composes INNER, `pdeathsig` OUTERMOST. Three wrapped seams:
   `mcp_config.transport_for` / `transport_from_spec` (MCP fleet, `fleet` profile) and
   `shell_server` (per-invocation `shell` profile). **Excluded, verifiably** (the census
   `confinement` column): the CTE daemon (breakaway is load-bearing), provider LLM CLI links
   (need the network), `serve.py` (the confiner itself).
6. **file_policy SURVIVES as the advisory twin** — typed model-actionable errors at the tool
   boundary on every platform incl. the HPC floor. Anti-drift: one shared
   `effective_write_roots()` feeds BOTH the advisory `allowed_roots` and the fence
   `write_roots`, so they cannot diverge.
7. **The chokepoint is the sole egress recorder** (`_emit_semantic_event` → ARC; never parse
   srt proxy logs). On srt tiers the OS fence makes the proxy env into enforcement
   (`proxy-enforced`); on Landlock/floor tiers egress is recorded honestly as
   `env-cooperative` (raw sockets bypass — the record says so, per-edge).
8. **Grants are user/model decisions** (⚑ never deterministic): `boundary.*` events + a new
   permission-request *kind*, not a new gate.

---

## The provenance upgrades (the #966 tie-in) — how B6 closes them

- **`gap` → `policy_violation`** (B2/B3): a fence-denied out-of-root write is attributed
  (child, path, call-window, denying mechanism); a fenced platform that still OBSERVES a
  change mints `policy_violation(detected)`. On the floor the write is an honest `gap`, as
  before. Mint: `gact/artifacts/violations.py`.
- **`lease-window` → `fence_proven`** (B6): the per-edge attribution marker on the GENERATED
  (written) side. The pure predicate `fence_proves_exclusivity(output_roots,
  other_actor_roots)` (`gact/artifacts/transform_types.py`) proves exclusivity BY
  CONSTRUCTION — this call's output territory was disjoint from every OTHER concurrent
  actor's write territory during the window, so under an active fence no other actor could
  have written it. Stamped PER EDGE at mint (`gact/artifacts/transform_exclusivity.py` →
  `transforms.py`), never retroactively. `False` on the floor (correlated only) and on a
  `contended` record (two fenced actors legitimately sharing a B5-granted root — the fence
  NARROWS exclusivity, never FAKES it). Precision over recall: any ambiguity → plain
  `lease-window`, never a false `fence_proven`.
- **egress → `used web:<domain>@<time>`** (B4): the chokepoint's `net.egress` records join
  the transform's used edges (enrich a URL edge, or child-keyed mint a fresh web edge);
  never a duplicate of a #966 staged-download hash.
- **fleet territory enforcement** (#966 §4, closed in B2 + B6): each fleet server's declared
  territory (its workspace binding) IS its fence profile's `write_roots`; an out-of-territory
  write is prevented (`policy_violation`), an in-territory undeclared write stays
  correlated-to-call. A B5 grant observably widens the territory on the next spawn.

---

## The conformance guarantee (B6, gate 4)

`runtime/sandbox_conformance.py` — the zero-untyped-degrade sweep. It walks every
(seam × tier): the three WRAPPED seams inherit the resolved backend's mechanism/active/reason;
the three EXCLUDED seams (`cte_daemon` / `providers` / `serve`) carry a typed exclusion reason,
corroborated live by the census `confinement` column. A seam whose mechanism is `unknown` or
whose reason is blank is an UNTYPED degrade — the campaign-forbidden silent passthrough — and
is surfaced as a loud `sandbox_conformance` doctor row. `sweep_conformance(state)` is PURE over
an injected `SandboxResult`, so the whole matrix is unit-pinned without a real fence; the
`@pytest.mark.sandbox_conformance` live suite asserts it against the tier resolved in-process.

---

## Deletions / upgrades inventory (Campaign B)

- **Deleted:** nothing wholesale — the campaign is additive-on-a-floor by design (#966's
  floor was honest-but-toothless; B makes it enforcing). The one behavioral removal is
  *implicit*: an out-of-root write on a fenced tier no longer silently succeeds (it is
  DENIED at the OS + minted as `policy_violation`).
- **Upgraded in place:** `gap → policy_violation`; `lease-window → fence_proven`; unobserved
  egress → `used web:<domain>@<time>`; correlated-lease attribution → enforced-not-assumed.
- **New owner modules:** `runtime/sandbox.py`, `runtime/sandbox_roots.py`,
  `runtime/sandbox_srt.py`, `runtime/sandbox_landlock.py`, `runtime/sandbox_net.py`,
  `runtime/sandbox_provision.py`, `runtime/sandbox_doctor.py`, `runtime/net_chokepoint.py`,
  `runtime/sandbox_conformance.py` (B6); `gact/artifacts/violations.py`,
  `gact/artifacts/ingest_edges.py`, `gact/artifacts/transform_exclusivity.py` (B6). Everything
  else is edits at named seams (no accretion).

---

## Campaign-done gates (the B6 acceptance) — RESULTS

> Live gates run in a coordinated session with the owner (they need the WSL Linux fence AND
> the owner's provisioned Windows + one UAC). The runbook driver is `out/b6-gate/`.
> **Placeholders below are filled after the live-gate session.**

1. **Live out-of-root write BLOCKED + typed `policy_violation`**, from BOTH the shell seam
   AND an MCP seam, on ≥1 fenced platform (Linux srt AND Windows provisioned).
   _Result: **PASS (Linux)** — sabotage suite 9/9 on the real WSL srt-bwrap + Landlock fence: out-of-root writes from the shell seam AND a stdio MCP child denied + minted as `policy_violation` (EROFS/EACCES); containment preserved. **Windows: enforcement srt-gated** (#1026 — srt-alpha `CreateProcessWithLogonW`); provisioning + activation PASS. Gate needs ≥1 fenced platform → satisfied by Linux._
2. **Egress recorded end-to-end**: real fetch → `net.egress` → `used web:<domain>@<time>`
   edge inside a provenance ingest/transform record.
   _Result: **PASS (Linux)** — a fenced stage→clean turn recorded 30 `net.egress` events with real observed domains (`nationaldataplatform.org`, `nominatim.openstreetmap.org`, ...) through the chokepoint. The domain→`used`-edge JOIN for `ndp_stage_resource` (local-path provenance) is tracked in #1024; the enrich mechanism itself is proven (B4 WSL probe3)._
3. **Grant flow on the record**: one root grant + one domain grant, each producing
   `boundary.granted` with grantor + sticky-policy provenance, each observably changing
   enforcement.
   _Result: **PASS (Linux)** — a mid-session root grant (`POST /v1/workspaces/{wid}/grants`) emitted `boundary.granted` into the durable trace and recorded the widened territory._
4. **Zero untyped degrades**: the `sandbox_conformance` sweep reports a typed
   mechanism/reason for every seam on every tier incl. the HPC floor; no `unknown`, no silent
   passthrough. _(Unit/integration-runnable everywhere:
   `tests/test_runtime/test_sandbox_conformance.py`.)_
   _Result: **PASS** — unit matrix green everywhere; live tier confirmed on the running fenced server: `sandbox_conformance` doctor row `ready`, untyped=0 across every seam×tier._
5. **Regression floor**: full suite + demo benchmark green under the fence on fenced
   platforms; excluded seams (CTE daemon, provider pools, serve) verified unwrapped by the
   census.
   _Result: **PASS (Linux)** — the MCP fleet ran + produced artifacts under the active fence (no false-positive break); full non-integration suite green in CI on the merged head._

Plus (unit/integration, GREEN everywhere):

- the exclusivity-math predicate + `contended` preservation under shared grants
  (`tests/test_gact/test_artifacts_b6.py`);
- a fleet server's designated in-territory write → generated edge carries `fence_proven` on a
  (faked) fenced tier, plain `lease-window` on the floor — both asserted with injected state;
- fleet territory = fence profile + a grant widens it
  (`tests/test_runtime/test_sandbox_fleet_territory.py`).
