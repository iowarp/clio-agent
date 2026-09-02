# case14 — Deep Researcher (Web Synthesis)

**The question family:** given only a human-level research request (no expert,
tool, or MCP vocabulary), can the marketplace `deep-researcher` pack — a flat
coordinator that dynamically fans out `researcher` leaves, sends the assembled
evidence through an independent `critic` leaf, and produces a cited Markdown
report — actually run end-to-end on clio-agent through the unified MCP client?

**Why it matters:** this is **not** a domain-discovery grind like EarthScope,
wildfire, or case13. Those cases build and refine an agent's own reasoning
contract against live scientific data. This case's job is narrower and more
load-bearing: it is **acceptance leg (iii)** of the MCP-client-unification
campaign (`docs/design/mcp-client-unification-2026-08.md`, issue
[#1286](https://github.com/iowarp/clio-agent/issues/1286), umbrella #1274) —
the user-visible proof that the generic MCP v2 client (built across C1-S1..S5
on `feat/mcp-client-unification`) actually lets a `task=required` server (the
clio-kit `web` MCP: `web_search` / `web_fetch`) work through a real,
unmodified marketplace pack. The pack's own AGENT.md files (researcher +
critic) already declare the evidence discipline (FACT vs INFERENCE labeling,
citation-to-claim mapping, an independent critic re-verification pass); this
case does not re-author that reasoning, it proves clio's MCP plumbing lets the
pack actually exercise it.

**Substrate:** `external/clio-agent-marketplace/deep-researcher` (submodule,
already authored upstream — not built by this case) installed via
`marketplace_source` as a **local filesystem path** (no network fetch during
install), talking to the clio-kit `web` MCP server declared in its own
`AGENT.md` frontmatter (`mcp_servers: { web: clio-kit mcp-server web }`).

**Topology (owned by the pack, not this case):** `main` (tier 1, react
coordinator) dynamically spawns any number of `researcher` (tier 2) and
`critic` (tier 2) direct children via `spawn_agent_task` /
`spawn_agents_parallel` — the C1-S1 capability-keyed spawn runtime, not a
declared workflow. `main` never calls `web_search`/`web_fetch` itself; only
the leaves do, each on its own child session.

**The research prompt** (`prompt.txt`): a primer on the HDF5 (Hierarchical
Data Format 5) scientific data format — what problem it solves, who maintains
it, its core data model, and its adoption across scientific/engineering
domains. Chosen for stability, not difficulty:

- **Long-lived, non-time-sensitive facts.** HDF5 has been maintained by The
  HDF Group since the early 2000s; "what is HDF5 / who maintains it / what is
  its data model" does not change month to month, unlike news, prices, or
  software-release-cadence questions.
- **A canonical, non-paywalled maintainer page exists** (The HDF Group's own
  HDF5 documentation), giving the `researcher`/`critic` leaves a concrete,
  fetchable primary source rather than only search-snippet-level evidence.
- **Independently verifiable adoption claims** (major scientific and
  engineering fields that rely on HDF5 for large-scale data storage) give the
  `critic` leaf real material to independently re-search and re-fetch, per its
  own AGENT.md validity condition.
- **Thematically adjacent but not overlapping** with the campaign's own
  fleet-domain breadth (hdf5/parquet appear in leg (i)'s canonical fleet list,
  `scripts/provenance_qualification/Dockerfile:13-16`) without this case
  reusing any NDP/geo/hdf5-tool machinery itself — it is a pure open-web
  research case, proving the generic MCP client, not a domain tool.

**Manual solution equivalent:** none authored here — HDF5's maintainer,
history, and data model are well-documented, easily-verified public facts;
the grind's job is proving the plumbing (MCP v2 `task=required` fetch reaching
a real marketplace pack), not grounding a hard scientific question.

## Semantics To Prove

- The coordinator dynamically delegates real web research (no forced worker
  count, no declared workflow) and at least one `researcher` leaf performs a
  genuinely successful `web_search` and `web_fetch`.
- The `critic` leaf's own validity condition holds: it independently calls
  `web_search`/`web_fetch` itself rather than merely approving the
  researcher's claims (its AGENT.md: "A critic pass is invalid unless you
  independently call `web_search` or `web_fetch`").
- A durable, cited Markdown report artifact lands in the session's artifact
  registry (the pack's completion gate: `create_artifact` after critic PASS).
- All of the above ride the **generic** MCP v2 client path (the gateway
  ProxyClient mount, not a privileged/hardcoded pathway) — this is the exact
  real-user failure the campaign exists to fix (#1274, `-32021`).

## Current Core Problem

This case is **scaffolding only** as of its authorship (C1-S6 slice
`slice/c1-s6-deep-researcher-case`): the case directory and
`tests/test_real_cases/test_deep_researcher_case.py` exist and collect
cleanly, but **no live run has been made** — the acceptance leg (iii) live run
happens at the campaign's own C1-S6 gate, after C1-S2..S5 land on
`feat/mcp-client-unification`. Per issue #1286: "RED today by construction —
it IS the honest #1274 repro." Grinding this case live before that point would
either fail for the exact reason the campaign exists (v2 `task=required`
fetch unreachable through the generic client) or require standing up
providers/servers outside this slice's scope.

**Status:** not run. Grind contract in `GOAL.md`; run logs (once a live run
happens) go in `runs/`.
