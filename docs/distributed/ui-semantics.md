# New semantics for the UI team (distributable runtime + context plane)

This release changes what CLIO *is*: from a single process that runs experts in-line, to a
**distributed expert system** — a parent agent delegating to isolated worker processes over
clio-core, optionally across cluster nodes. The UI (TUI / web / desktop) should grow to
*represent* that: a view of clio → nodes → experts, messages in flight, where each expert runs,
and the context that flows between them.

This doc is the contract to design against: the new entities, what data is **consumable today**,
and the **endpoints we should add together** (marked PROPOSED — let's co-design the exact shape).

---

## 1. The new mental model

```
  Session (a conversation)
     └─ Parent agent  ── delegates ─►  Expert delegation (a "message")
                                          routed to ▼
  Deployment (a cluster)
     ├─ Node 0 ── clio_run daemon ──┬─ worker (role=data,  #0)  ◄─ runs an expert child
     │                              └─ worker (role=data,  #1)
     ├─ Node 1 ── clio_run daemon ──┬─ worker (role=analysis,#0)
     │                              └─ worker (role=data,  #2)
     └─ …                           (workers announce PRESENCE; parent routes to live ones)
```

Two things are genuinely new for the UI:
- **Topology + location**: experts now run in *separate processes / nodes*. "Where is this
  expert running" is a real, answerable question.
- **Messages in flux**: a delegation is a request/result that physically crosses clio-core
  (a mailbox over CTE blobs) — observable as it's submitted, picked up, served, and returned.

(Single-process / co-located mode still exists; topology there is trivially "all local". The new
views matter once `CLIO_EXPERT_INVOKER=clio_core_isolated` + a worker fleet is deployed.)

---

## 2. Entity model (what to render)

| Entity | Key fields | Notes |
|---|---|---|
| **Deployment / cluster** | nodes[], shared config path | from the deployment yaml (`cluster:` section) |
| **Node** | host, addr, node_id, daemon reachable? | node_id = hostfile line order |
| **Daemon** (clio_run) | node, port, alive?, tiers/usage | one per node; CTE backend |
| **Worker** (expert instance) | role, node, worker_id, pid, **present?**, **busy?** | the unit that runs an expert child |
| **Role / pool** | role name, desired vs live replica count | role == the delegated expert id |
| **Delegation / message** | id, parent → role/worker, status (submitted→running→completed/failed), timing, answer summary | the "message in flight" |
| **Session / context** | session id, context segments/frames, what crosses each hop | the ARC live context plane |

Lifecycle of a **delegation** (the state machine the UI animates): `submitted` (parent wrote the
request) → `routed` (to a specific live worker) → `running` (worker serving) → `completed` /
`failed` → (parent resumes). Exactly-once: each runs on exactly one worker.

---

## 3. Data sources

### Consumable TODAY (already in the backend)
- **Semantic event stream (SSE)** — the TUI already consumes `semantic.event`s. Relevant types:
  `delegation.started` / `delegation.completed` / `delegation.failed` / `delegation.parent_resumed`
  (+ `blueprint.delegation.*`, `subagent.started/completed`). Each carries actor/subject
  `agent_id`, `execution_mode`, `delegation_lifecycle`, provider, blueprint. **This already drives
  a "who delegated to whom + status" view.**
- **Context plane** — `GET /v1/sessions/{sid}/context/frames` and `/context/files` expose the
  live context the agent reasons over (the ARC live-context-plane work in this RC).
- `GET /v1/agents`, `/v1/sessions`, `/v1/metrics`, `/v1/providers` — the existing catalog/state.

### PROPOSED — needs a new endpoint (let's co-design)
- **Topology** — `GET /v1/cluster/topology`: nodes (host/addr/node_id/daemon-alive) → workers
  (role/worker_id/pid/present/busy). Backend has all of this (`clio-cluster status`,
  `live_workers(role)`); it needs an HTTP surface. *This powers the clio→nodes→experts view.*
- **Worker/node attribution on delegation events** — today's delegation events say *which expert*,
  not *which worker / which node* it ran on. Adding `worker_id` + `node` (+ pid) to the
  `delegation.*` payloads is the smallest change that unlocks "where is this expert running".
- **Messages in flux** — two options: (a) derive from the delegation event stream (submitted→
  completed timing per message — no new backend needed beyond attribution); (b) for raw transport
  traffic, proxy **clio-core telemetry** (`CteTelemetry` / `PollTelemetryLog`) via
  `GET /v1/cluster/telemetry` to show blob ops / tier usage / in-flight counts per node.
- **Fleet health** — `GET /v1/cluster/workers`: per role, desired vs live replica counts,
  per-worker present/busy (presence heartbeats). Drives a fleet dashboard + restart/scale UX later.

---

## 4. Concrete views to build (mapped to the data above)

1. **Topology / "clio to nodes & experts"** — a tree or graph: cluster → nodes → daemon →
   workers (colored by role; lit when present, pulsing when busy). Source: `/v1/cluster/topology`.
2. **Message-in-flux** — a live flow (Sankey / animated edges): parent → worker, one edge per
   in-flight delegation, settling on completion; counts per role. Source: delegation event stream
   (+ attribution) and/or `/v1/cluster/telemetry`.
3. **Expert location** — for the current turn's delegations, badge each with the node/worker it
   ran on + duration. Source: delegation events + the proposed worker/node attribution.
4. **Fleet health** — per role: desired vs live workers, presence age, busy/idle. Source:
   `/v1/cluster/workers`.
5. **Context flow** — what context crosses each delegation hop (the reference/scope the child
   receives) and the shared blackboard on a cluster. Source: `/context/frames` + (proposed)
   per-delegation context attribution.

---

## 5. Context-management semantics (the other half of the RC)

- Context lives in the **ARC live context plane** — ordered, scoped, mutable segments the agent
  reads its prompt from each step (not a concatenated history). Already surfaced via
  `/v1/sessions/{sid}/context/frames`.
- **Context crosses delegation boundaries**: a child expert receives scoped parent context +
  routing, and its result (answer + `workflow_state` + the routing decision) folds back — verified
  end-to-end (a reference code planted in hop 1 survives into every downstream hop, including
  concurrent children).
- On a **cluster**, that context lives in the **shared clio-core CTE blackboard** (one logical
  store across the daemon mesh) rather than one process's memory. The UI's "context flow" view is
  where this becomes visible/legible.

---

## 6. Stability — build on these, co-design the rest

- **Stable now**: the semantic event stream (delegation/subagent events) and the
  `/v1/sessions/{sid}/context/frames|files` surfaces. Safe to build against today.
- **Co-design (not built yet)**: `/v1/cluster/topology`, `/v1/cluster/workers`,
  `/v1/cluster/telemetry`, and the worker/node attribution on delegation events. These are small,
  well-scoped backend additions — tell us the exact fields/shape your views need and we'll expose
  them to match, rather than guessing the contract.

The deployment + transport details behind this (how workers are placed, the mailbox, presence) are
in `docs/distributed/HANDOFF.md` and `docs/distributed/cluster.md` — useful background, but the UI
only needs the entity model + data sources above.
