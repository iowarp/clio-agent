# CLIO Benchmark Readiness Index

Updated: 2026-06-03

This file is the short status boundary for the `benchmark/` directory.

## Source Of Truth

The final public-demo benchmark should be built from:

- `CLIO_HIERARCHICAL_AGENT_BENCHMARK_CASES_SOURCE.md`
- `CLIO_HIERARCHICAL_AGENT_BENCHMARK_REVIEW.md`

Those files define the target semantics: real scientific workflows, natural
prompts, meaningful expert hierarchies, sync delegation returns, fan-out/merge,
recovery, provenance, objective pass criteria, and ablations where useful.

## How To Read Existing Evidence

Current marketplace reports are useful runtime evidence, but they are not the
final benchmark content. They prove infrastructure contracts such as:

- Agent Blueprint install and per-session activation.
- Root-owned sync delegation and parent resume.
- Pack-local tools, skills, MCP descriptors, hooks, and workspace memory scope.
- Live event observation and benchmark evidence rendering.
- ALCF Metis provider execution through CLIO.

They should not be cited as the final scientific benchmark set. The early
marketplace packs were intentionally scaffold-like and some are too shallow for
the demo target.

## Current Strong Evidence

- `ALCF_GENOMICS_REFERENCE_DELEGATION_REPORT.md` proves merged CLIO develop plus
  merged marketplace main can execute a marketplace Agent Blueprint on ALCF
  Metis with `main -> reference -> main` and
  `reference -> reference_quality -> reference` sync delegation.
- `MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md` proves explicit trust, launch,
  probe, and call for a pack-local stdio MCP server.
- `MARKETPLACE_PACKAGED_HOOK_REPORT.md` proves explicit trust and invocation of
  a pack-local hook with semantic provenance.
- `MARKETPLACE_WORKSPACE_MEMORY_SCOPE_REPORT.md` proves workspace memory tool
  isolation and explicit same-workspace intent.

These are infrastructure proofs. They support the next benchmark wave; they do
not replace it.

## Replacement Work

Marketplace issue `JaimeCernuda/clio-agent-marketplace#33` tracks replacing or
rewriting the scaffold packs with researched scientific benchmark agents.

Initial replacement targets:

- Genomics cohort QC.
- Proteomics LFQ differential abundance.
- HPC I/O regression.
- Scientific format bridge integrity.
- Terrain/lidar suitability.
- NDP-backed collection/recovery workflow.

Each replacement pack should have a paired CLIO benchmark case, objective pass
criteria, pack-local skills, meaningful expert hierarchy, and watched
real-provider evidence.

## Do Not Overclaim

Do not describe the current benchmark folder as a completed full public-demo
benchmark. It currently contains:

- design sources,
- infrastructure evidence,
- historical evidence,
- first-wave/scaffold evidence,
- focused ALCF and semantic-regression traces.

The next milestone is to replace the scaffold packs and rerun the watched
benchmark with the researched packs.
