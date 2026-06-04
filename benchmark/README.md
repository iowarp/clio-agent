# CLIO Scientific Benchmark

This directory contains the reusable benchmark prompt book for CLIO's real
provider scientific workflow runs.

The benchmark is not an ALCF-only demo. ALCF Metis/Sophia runs are one evidence
lane, alongside local LM Studio/Qwopus and future provider lanes. The benchmark
contract is the prompt family, expected routing/tool behavior, artifacts, memory
semantics, and surfaced-error behavior.

## Files

- `BENCHMARK_READINESS_INDEX.md` - short status boundary for this directory:
  which files are final benchmark sources, which reports are infrastructure
  evidence, and why the early marketplace packs must be replaced by researched
  scientific benchmark agents.
- `CASE_EVIDENCE_CONTRACT.md` - required evidence layout for the 12 public-demo
  benchmark case folders. A case is not passed until its folder contains live
  streamed events, semantic trace, run metadata, outputs, and a human result
  note.
- `caseXX-short-name/` - per-case benchmark evidence folders tracked by
  `iowarp/clio-agent#628`. These folders start as contracts and should be
  filled by watched live runs, not by unit-test output.
- `CURRENT_STATUS.md` - current human-facing benchmark status: what failed,
  what was fixed, what now works, and which stress gaps remain future work.
- `REAL_PROVIDER_PROMPTS.md` - collaborator-grade prompts and expected behavior.
- `BENCHMARK_PROMPT_BOOK.md` - concise manual TUI prompt book and pass criteria.
- `CROSS_DOMAIN_DEMO_RESULTS.md` - human-facing evidence index and acceptance
  matrix.
- `FRESH_REAL_ORCHESTRATOR_REPORT.md` /
  `FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` - historical isolated replay
  evidence retained for debugging; this report is superseded for current
  NDP/seismic status by `MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md`.
- `MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md` /
  `MARKETPLACE_COMPLEX_HIERARCHY_EVIDENCE.jsonl` - June 3 full marketplace run
  after stricter complex hierarchy criteria. This report proves five complex
  marketplace hierarchy cases and records the geospatial delegation-prose
  failure that was fixed immediately afterward.
- `MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md` /
  `MARKETPLACE_GEOSPATIAL_RETRY_EVIDENCE.jsonl` - focused June 3 retry for the
  only failed full-run case. It proves the corrected geospatial pack performs
  real `main -> spatial_features -> main` sync delegation and tool calls.
- `MARKETPLACE_MCP_SCOPE_REPORT.md` /
  `MARKETPLACE_MCP_SCOPE_EVIDENCE.jsonl` - focused June 3 semantic-regression
  evidence for a marketplace pack-local MCP descriptor. It proves the
  `mcp-calculator-smoke` pack exposes disabled-by-default descriptor metadata,
  pack-local stdio launch derivation, `calculator_add` scope, and explicit
  trust requirements. It does not claim enabled MCP tool execution.
- `MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md` /
  `MARKETPLACE_MCP_ENABLED_EXECUTION_EVIDENCE.jsonl` - focused June 3
  semantic-regression evidence for enabled marketplace MCP execution. It proves
  the `mcp-calculator-smoke` descriptor can be explicitly trusted, launched as a
  real pack-local stdio FastMCP server, probed to ready, and called through
  CLIO's MCP call endpoint. This is an action-only infrastructure proof, not a
  model-turn or hierarchy-depth proof.
- `MARKETPLACE_PACKAGED_HOOK_REPORT.md` /
  `MARKETPLACE_PACKAGED_HOOK_EVIDENCE.jsonl` - focused June 3
  semantic-regression evidence for marketplace packaged hook invocation. It
  proves the `hook-smoke` pack exposes a disabled-by-default `pre_message` hook
  descriptor, explicitly trusts/enables it, and records live
  `hook.pre_message.blocked` semantic events with packaged Agent Blueprint
  provenance. This is an action-only infrastructure proof, not a model-turn or
  hierarchy-depth proof.
- `MARKETPLACE_WORKSPACE_MEMORY_SCOPE_REPORT.md` /
  `MARKETPLACE_WORKSPACE_MEMORY_SCOPE_EVIDENCE.jsonl` - focused June 3
  semantic-regression evidence for workspace memory scope. It proves
  cross-session memory search is denied without explicit intent,
  same-workspace memory search succeeds with intent and provenance, and
  another workspace's session summary is denied.
- `ALCF_GENOMICS_REFERENCE_DELEGATION_REPORT.md` /
  `ALCF_GENOMICS_REFERENCE_DELEGATION_EVIDENCE.jsonl` - focused June 3 ALCF
  Metis evidence after merged CLIO and marketplace fixes. It proves Agent
  Blueprint wrapper-agent evidence handling plus root-owned sync delegation for
  `genomics-review`. This is infrastructure evidence, not final benchmark
  content.
- `MARKETPLACE_UNIFIED_REPORT.md` /
  `MARKETPLACE_UNIFIED_EVIDENCE.jsonl` - historical May 29 evidence for
  marketplace Agent Blueprint loading, root-owned delegation, and one complex
  seismic hierarchy. Superseded by the June 3 complex hierarchy evidence set for
  current status.
- `scripts/run_demo_benchmark.py` - executable runner that materializes these
  cases against a live GACT backend.
- `scripts/create_benchmark_data.py` - deterministic local data generator.
- `docs/ALCF_DEMO_BENCHMARK_REPORT.md` - historical ALCF evidence report from a
  real `argonne / gpt-oss-120b` run.
- `docs/STRESS_BENCHMARK_REPORT.md` - historical local and ALCF verification
  report.

## Data Bundle

Generate the deterministic local benchmark files from the repo root:

```bash
uv run python scripts/create_benchmark_data.py --output-dir tmp/clio-benchmark-data
```

The prompt book uses these logical placeholders:

- `{h5}` - `tmp/clio-benchmark-data/fusion_run.h5`
- `{parquet}` - `tmp/clio-benchmark-data/facility_measurements.parquet`
- `{dirty}` - `tmp/clio-benchmark-data/facility_measurements_dirty.parquet`
- `{csv}` - `tmp/clio-benchmark-data/sensor_events.csv`
- `{adios}` - `tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5`

## Run

Start a GACT backend with a real provider, then run:

```bash
uv run python scripts/run_demo_benchmark.py \
  --require-lane-criteria \
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl tmp/clio-real-orchestrator-benchmark.jsonl \
  --report benchmark/REAL_ORCHESTRATOR_REPORT.md
```

The default lane is `real_orchestrator`; it fails shortcut route sources such as
`guard`, `user_agent_keyword`, and `recovery`. To run the older broad all-cases
campaign explicitly, use:

```bash
uv run python scripts/run_demo_benchmark.py \
  --lane all \
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl tmp/clio-real-provider-benchmark.jsonl \
  --report docs/ALCF_DEMO_BENCHMARK_REPORT.md
```

New strict benchmark evidence should normally stay under this `benchmark/`
directory. Historical provider-specific reports can remain under `docs/`.

The early marketplace packs and reports in this folder are not the final
scientific benchmark target. They are retained because they exposed runtime
defects and prove infrastructure contracts. Replacement of those scaffold packs
with researched scientific benchmark agents is tracked in
`JaimeCernuda/clio-agent-marketplace#33`.

The `marketplace_agents` lane separates smoke coverage from complex hierarchy
coverage. Pack loading, root delegation, and tool use are necessary but not
enough: `--require-lane-criteria` now also requires at least three marketplace
cases with depth >= 3, branch count >= 2, sync handoff count >= 2, and complete
parent-return provenance.

The current committed June 3 marketplace evidence is a combined evidence set,
not a single all-green full-lane rerun after every fix. Read it as:

1. `MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md` proves the full lane reached five
   complex hierarchy passes after criteria correction and isolated the
   geospatial failure. Its JSONL preserves the original runner verdict fields
   from before the report was re-rendered.
2. `MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md` proves that remaining geospatial
   failure was fixed with live sync delegation and tool-call evidence.

A final public-demo dry run should still regenerate the full lane from a fresh
machine/session once the benchmark pack set is frozen.

The MCP scope and enabled-execution reports are intentionally focused
semantic-regression cases. The scope report proves CLIO surfaces descriptor,
launch metadata, and trust state before enablement. The enabled-execution report
proves explicit trust/enable/probe/call with a real pack-local MCP server. The
packaged-hook report proves CLIO can invoke an explicitly enabled pack-local
hook with semantic trace provenance. Both are action-only by design so provider
availability does not mask the underlying runtime contracts.

The workspace-memory report is also intentionally action-only. It proves the
memory policy/tool contract directly through GACT endpoints, not through
provider prose.

Before running the expensive marketplace lane, run the static preflight against
the marketplace source. This uses CLIO's Agent Blueprint validator for every
pack and fails if fewer than the requested number of non-seismic packs have a
real nested hierarchy:

```bash
uv run python scripts/validate_marketplace_blueprints.py \
  /path/to/clio-agent-marketplace \
  --require-complex-count 3 \
  --exclude-complex-id seismic-waveform-review \
  --output benchmark/MARKETPLACE_PREFLIGHT_REPORT.md
```

The `semantic_regression` lane is the 1.0 readiness gate for the semantics that
were easy to overclaim during deterministic-routing removal. Each case declares
semantic proof tags, the JSONL row records which tags were actually observed,
and `--require-lane-criteria` fails if any required proof class is missing.

```bash
CLIO_SEMANTIC_TRACE_BACKEND=file \
CLIO_SEMANTIC_TRACE_DETAIL=semantic \
uv run python scripts/run_demo_benchmark.py \
  --require-lane-criteria \
  --lane semantic_regression \
  --marketplace-source /path/to/clio-agent-marketplace \
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl benchmark/SEMANTIC_REGRESSION_EVIDENCE.jsonl \
  --report benchmark/SEMANTIC_REGRESSION_REPORT.md
```

## Audit

Treat the markdown report as an index, not the proof. The proof is the JSONL
row for each case, especially `session_log.root_messages` and
`session_log.child_sessions[].messages`. A human audit should check that the
natural prompt, selected route, delegated experts, tool calls, surfaced errors
or recoveries, artifacts, and final answer all agree with each other.

For the 12-case public-demo benchmark, use the case-local evidence folders
defined in `CASE_EVIDENCE_CONTRACT.md`. Historical flat reports in this
directory can support debugging or migration, but the `#628` checklist should
only be checked off after the matching `caseXX-short-name/` folder contains the
live run trace, streamed event capture, semantic log, outputs, and result note.
