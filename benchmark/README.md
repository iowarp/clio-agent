# CLIO Scientific Benchmark

This directory contains the reusable benchmark prompt book for CLIO's real
provider scientific workflow runs.

The benchmark is not an ALCF-only demo. ALCF Metis/Sophia runs are one evidence
lane, alongside local LM Studio/Qwopus and future provider lanes. The benchmark
contract is the prompt family, expected routing/tool behavior, artifacts, memory
semantics, and surfaced-error behavior.

## Files

- `CURRENT_STATUS.md` - current human-facing benchmark status: what failed,
  what was fixed, what now works, and which stress gaps remain future work.
- `../docs/PREBENCHMARK_HARDENING_AUDIT.md` - backend hardening matrix for
  deciding which surfaces are implemented, tested, and still awaiting
  real-provider proof before 1.0 readiness language.
- `../docs/DETERMINISTIC_SHORTCUT_AUDIT.md` - classification of deterministic
  paths that are allowed only as verification, guardrail, or explicit legacy
  infrastructure.
- `REAL_PROVIDER_PROMPTS.md` - collaborator-grade prompts and expected behavior.
- `BENCHMARK_PROMPT_BOOK.md` - concise manual TUI prompt book and pass criteria.
- `CROSS_DOMAIN_DEMO_RESULTS.md` - human-facing evidence index and acceptance
  matrix.
- `FRESH_REAL_ORCHESTRATOR_REPORT.md` /
  `FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` - historical isolated replay
  evidence retained for debugging; this report is superseded for current
  NDP/seismic status by `MARKETPLACE_UNIFIED_REPORT.md`.
- `MARKETPLACE_UNIFIED_REPORT.md` /
  `MARKETPLACE_UNIFIED_EVIDENCE.jsonl` - current passing evidence for
  marketplace Agent Blueprint loading, root-owned delegation, and one complex
  seismic hierarchy. The strict marketplace lane gate now requires at least
  three complex marketplace hierarchy cases before the report can be used as
  broad hierarchy coverage.
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

The `marketplace_agents` lane separates smoke coverage from complex hierarchy
coverage. Pack loading, root delegation, and tool use are necessary but not
enough: `--require-lane-criteria` now also requires at least three marketplace
cases with depth >= 3, branch count >= 2, sync handoff count >= 2, and complete
parent-return provenance.

## Audit

Treat the markdown report as an index, not the proof. The proof is the JSONL
row for each case, especially `session_log.root_messages` and
`session_log.child_sessions[].messages`. A human audit should check that the
natural prompt, selected route, delegated experts, tool calls, surfaced errors
or recoveries, artifacts, and final answer all agree with each other.
