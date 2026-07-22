# Deterministic artifact provenance — revised design (v2)

**Status:** design notes, 2026-07-16. Revision of the owner+Cowork design
(`artifact-provenance-design.md`, Cowork session outputs 2026-07-16) after an adversarial
review; every criticism from that review is resolved in-text. Companion to the semantics
survey `docs/design/artifacts-research-2026-07.md`.

**Scope decision (owner question, 2026-07-16):** the grown scope — (a) sandboxing,
(b) expanded artifacts, (c) provenance — is TWO campaigns, not one and not three:

- **Campaign A — Artifacts** = (b) + (c)-floor. Registry + versioning + wire identity +
  designation + transform records on the universal floor. No sandbox dependency; every
  degradation labeled. This document's Layers 1–2.
- **Campaign B — Sandboxing** = (a) + (c)-upgrade. Write confinement, grant UX, network
  chokepoint, optional container substrate. Acceptance expressed in provenance terms:
  gaps become violations, correlated windows become exclusive by construction. This
  document's Layer 3.

Rationale: (b)+(c) share one schema decision and one mint point (splitting them forfeits
the provenance seam in the registry design); (a) is a different code surface and risk
class whose only coupling to (c) is raising its guarantee ceiling. A follows #948
(attribution consumes task records + leases). B can slot anywhere after A — tier labels
make its absence visible, never silent.

## 1. Review resolutions (what changed from v1)

| # | Criticism | Resolution |
|---|---|---|
| 1 | Traced tier contradicts the declared-beats-observed graveyard lesson; container-as-default was justified by the weakest tier | §5: v1 capture = floor + exact + tool-declared + correlated. Tracing demoted to a future upgrade admitted only with a designated-set read filter. The container is re-justified by **environment capture** (image digest = exact env identity), not tracing |
| 2 | Transform record missing environment identity — DVC's own notorious repro failure | §3: `environment` field, itself tiered (`declared` → `lockfile-hash` → `image-digest`). A replay claim requires env tier ≥ lockfile-hash; below that the record says *re-runnable*, not *reproducible* |
| 3 | Concurrency breaks window-based correlated attribution (#948 guarantees overlap) | §4: attribution is **lease-scoped, not window-scoped**. Correlated records require territory exclusivity proven by an existing lease (per-namespace serialized executors, #933 workspace leases, #949 turn leases, per-child task workspaces). Overlap without a lease → `contended` record naming the candidate set — never false certainty |
| 4 | Persistent MCP fleet doesn't fit the per-invocation boundary model | §4: fleet-shaped answer — per-server declared territory (workspace binding, `execution.py:85`), per-call windows serialized within a namespace by the sync executor, `tool-declared` records minted at the grounding hook (`execution.py:1176`) with hash-verified declared outs |
| 5a | "Immutability guaranteed" CAS overclaim in no-container mode | §3: custody classes are themselves tiered — `isolated` / `app-private` / `referenced`. The honest universal guarantee is **detection** (hash self-validation makes corruption visible), prevention is per-class |
| 5b | Hash-at-read "essentially free" is false for multi-GB scientific data | §3: identity evidence classes — `hashed-at-use` / `authority-asserted` (DOI, registry checksum, ETag) / `stat-pinned` (labeled weakest). Ingest tools hash **while streaming** (downloads get exact hashes at zero extra I/O) |
| 6 | Single tier ladder invites misreading as a total confidence order | §4: split into `mechanism` (what produced the record) + **per-edge evidence** (each used/generated edge carries its own basis). Badges are computed per question ("outputs verified?" vs "deps complete?"), never stored as a scalar |

Unchanged from v1 (defended in review): LLM never load-bearing in custody; designation not
discovery; content-hash identity surviving custody gaps; coarse boundary-only transform
records; gap nodes; label-don't-warn; PROV as export vocabulary only; RO-Crate bundle
export; A2A/MCP wire shapes; UI payloads as an artifact kind.

## 2. Principles (inherited)

1. The model never sits in the chain of custody. Records come from the harness, hashing,
   or the OS; the model only annotates (intent, deliverable-vs-scratch), visibly untrusted.
2. Cross-platform: single-platform mechanisms are upgrade tiers, never requirements.
3. Users keep native access to their files.
4. Honest degradation: weaker guarantees ride on the record permanently, per edge.

## 3. Core model

### Designation, not discovery
Artifacts are the curated meaningful set (deliverables, datasets, models, reports, plans
— `plan` is a reserved kind for a future planning capability (owner, 2026-07-16) — UI
payloads, transform specs). Designation channels, in precedence order (owner decision
2026-07-16 — free expert-pool agency is the primary mechanism going forward; blueprint
workflows are the demo-era secondary path): **tool declaration** (output-path args at the
grounding hook — automatic), **agent proposal** (a single `create_artifact` tool: by path
or inline content, harness-validated and harness-hashed, typed rejections the model can
react to; the inert `structured_outputs.artifacts` field is DELETED, not made real — no
dual pathways), **user pin**, and **pack declaration**
(`workflow_state.artifact_paths` — survives as an optional convenience for blueprint
workflows, never load-bearing). Observation scope is broad; the versioned set is small.

### Identity and versioning (W&B semantics + evidence classes)
Content hash is identity; same name + new content → v(n+1); immutable versions, mutable
aliases; identity survives custody gaps (re-entry re-links by hash). Identity *evidence*
is classed and recorded:

- `hashed-at-use` — locally computed sha256 (streamed during ingest whenever bytes already
  flow through a tool; stat-cache for re-hash avoidance, distrusted on filesystems with
  unreliable mtimes).
- `authority-asserted` — DOI / registry checksum / S3 ETag / provider manifest. Cheaper
  than hashing 50 GB and often *stronger* provenance (points into the global data commons).
  The NDP metadata catalog is exactly this class.
- `stat-pinned` — size+mtime only; weakest, permanently labeled.

### The transform record
One coarse record per operation; the activity is opaque, only its boundary is recorded:

```
TransformRecord {
  activity_id, session_id, turn_id, time_start, time_end
  agent            // session/model/user; executing vs annotating agent distinguished
  instrument       // tool name + args, or {cmd, script_hash}; the script is itself an artifact
  environment      // {os, arch, tool/interpreter versions, lockfile_hash?, image_digest?}
                   //   env tier: declared | lockfile-hash | image-digest
  object[]         // inputs: artifact@version | external:path@identity-evidence
  result[]         // outputs: artifact@version | export manifest
  mechanism        // harness | tool-schema | change-feed | trace | model | none
  edges[]          // per-edge evidence: schema-arg | hash-pair | lease-window | authority | assertion
  annotation?      // model-provided intent — untrusted, never merged into edges
}
```

Replay contract: reproducible ⇔ env tier ≥ lockfile-hash AND all object identities pinned;
otherwise the record is re-runnable and says so. For CLIO the cheap env floor exists
already: uv lockfile hash + clio-kit launcher fingerprints (the listing-cache size:mtime
fingerprint) + provider/model ids.

### Custody classes (tiered, per resolution 5a)
- `isolated` — container/VM-private volume. Prevention against everything but the admin.
- `app-private` — CAS in an app-owned directory on the user's disk. Prevents agent- and
  accident-corruption; does NOT prevent a hostile local process. Corruption is always
  *detectable* (content-addressed store self-validates) — detection is the universal
  guarantee, prevention is the class.
- `referenced` — bytes stay external; identity pinned by evidence class at time of use.
  Lineage without custody (W&B reference artifacts).

Store discipline (CLIO): transform records and artifact metadata are a **projection over
the existing event log** (RULE 4 / #737 — no new store); only CAS bytes are new storage,
under a #930-style budget knob with reachability GC (keep everything reachable from pinned
aliases + export manifests).

## 4. Capture: mechanisms, attribution, and the fleet

### Mechanisms (not a ladder)
- `harness` (**exact**) — the harness executes the operation (Write/Edit/fs_write, staged
  downloads, WebFetch): edges from the operation itself. All platforms, forever.
- `tool-schema` (**tool-declared**) — MCP tools: the harness sees declared args, not actual
  I/O. Minted at the grounding hook: declared outs hash-verified (before/after), deps
  unknown unless declared. Honest middle tier — never presented as exact.
- `change-feed` (**correlated**) — territory-scoped file events + before/after hashes,
  attributed **only under a lease** (see below).
- `trace` — future upgrade, admitted only with a designated-set read filter answering the
  §1 graveyard objection. Never required.
- `model` (**declared**) — assertion only, quarantined.
- `none` (**gap**) — detected, unattributed change: explicit node attributed to an unknown
  external agent. Under Campaign B this class converts to a policy violation.

### Attribution under concurrency (resolution 3)
A correlated record is valid only when the runtime can prove no other activity could have
written the territory in the window — i.e. attribution rides an existing **lease**:
per-namespace MCP executors serialize calls (within a server, call windows never overlap);
child tasks get per-task workspace scratch (#948/#951); turn leases scope main-agent shell
windows. Overlapping activities on genuinely shared territory produce a `contended` record
carrying the candidate set {A, B} — the concurrency analogue of a gap node. False
attribution is treated as worse than no attribution.

### The MCP fleet (resolution 4)
Long-lived servers, not per-invocation children. Each server has a declared territory (its
workspace binding); each *call* is the activity (windows serialized per namespace by the
sync executor); declared outs verified by hash at the grounding hook; in-territory
undeclared writes during an exclusive call window → correlated-to-that-call; writes outside
any declared territory → gap (Campaign B: violation).

## 5. Layers → campaigns

- **Layer 1 — registry + wire (Campaign A, first slices).** ArtifactRef-shaped records
  (relay-#59-compatible: content pins, PROV-style used edges; add mechanism/edge-evidence
  via metadata until schemas converge), versioning, designation, outbound artifact
  events/parts (plots gain wire identity), routes, UI-payload artifact kind (record +
  delivery only — rendering belongs to the mcpui/a2ui campaign).
- **Layer 2 — provenance floor (Campaign A, later slices).** Transform records with
  environment; exact + tool-declared + lease-correlated capture; gap/contended nodes;
  authority-asserted identity; RO-Crate-shaped "give me the scripts" export; PROV export
  mapping (Entity/Activity/used/wasGeneratedBy/wasRevisionOf; gap nodes → unknown Agent).
- **Layer 3 — confinement (Campaign B).** Cross-platform write fence (file-policy →
  kernel/ACL prevention; srt-style host sandbox; container optional, justified by
  image-digest env identity), grants as recorded boundary-extension events, network
  chokepoint (egress control + `used web:URL@hash` ingest records), fleet territory
  enforcement. Gate: after B, the floor is complete (unwatched writes prevented), gap
  nodes become violations, correlated leases enforced not assumed.

## 6. Open questions (carried, reduced)

1. Designation acceptance policy for agent proposals (over-designation risk — pack
   declaration stays primary; a cost function or per-turn cap on promotions).
2. CAS budget defaults + GC cadence (#930 discipline; reachability roots = aliases +
   export manifests).
3. Relay schema convergence timing for mechanism/edge-evidence fields (ride `metadata`
   until a relay minor rev).
4. Whether Campaign B's container mode ships as repro-substrate-only (env identity) or
   also revives tracing behind the read filter.
