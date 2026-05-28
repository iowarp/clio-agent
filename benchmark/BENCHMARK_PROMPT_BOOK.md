# CLIO Real-Orchestrator Benchmark Prompt Book

This is the human-facing benchmark set for manual TUI runs on a fresh machine.
Use a real provider and the normal CLIO orchestrator. The benchmark should not
be judged as passing if a case succeeds only through shortcut routing, keyword
routing, guard routing, or recovery routing.

## Setup

Generate the local fixture data from the repo root:

```bash
uv run python scripts/create_benchmark_data.py --output-dir tmp/clio-benchmark-data
```

Use these paths when replacing placeholders:

- `{h5}`: `tmp/clio-benchmark-data/fusion_run.h5`
- `{parquet}`: `tmp/clio-benchmark-data/facility_measurements.parquet`
- `{dirty}`: `tmp/clio-benchmark-data/facility_measurements_dirty.parquet`
- `{csv}`: `tmp/clio-benchmark-data/sensor_events.csv`
- `{adios}`: `tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5`

For an automated evidence run, use:

```bash
uv run python scripts/run_demo_benchmark.py \
  --require-lane-criteria \
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl tmp/clio-real-orchestrator-benchmark.jsonl \
  --report benchmark/REAL_ORCHESTRATOR_REPORT.md
```

## Pass Standard

A passing run should show:

- `routing_decision.metadata.route_source` is not `guard`, `user_agent_keyword`,
  or `recovery` for real-orchestrator cases.
- Real tool evidence is visible in the turn metadata or transcript.
- Multi-branch prompts produce child sessions, expert handoffs, or equivalent
  provenance for the involved experts.
- Visualization cases produce a real PNG path when data is available.
- NDP cases either stage/analyze a bounded resource or return an honest
  unavailable/too-large result without inventing downstream artifacts.

## 1. Cross-File Triage

Agent: Data Exploration/Search Agent

Prompt:

```text
I have four related files from the same experiment: {h5}, {parquet}, {csv}, and "{adios}". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

Why this exists:

This is the core hierarchy prompt. It asks for a collaborator-ready synthesis
without naming the route. The orchestrator should decompose the work across data
and analysis capabilities instead of answering from text alone.

Worked because you saw:

- HDF5, Parquet, CSV, and ADIOS/BP5 inspection evidence.
- Tier-3 worker, child-session, or expert handoff provenance.
- A final readiness summary that cites each file and names follow-up checks.

## 2. Dirty Cross-File Quality Gate

Agent: Data Exploration/Search Agent

Prompt:

```text
Before I share this run, build a quality gate across {h5}, {dirty}, {csv}, and "{adios}". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

Why this exists:

This is the harder multi-branch version. The prompt asks for a review gate, not a
generic summary, and one source is intentionally dirty.

Worked because you saw:

- Tool-backed findings from clean scientific files and the dirty Parquet export.
- Explicit quality concerns tied to fields such as `quality_flag`,
  `temperature_k`, or `pressure_pa`.
- A blocker list for collaborator handoff.

## 3. NDP Seismic Waveform To Plot

Agent: Data Exploration/Search Agent

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

Why this exists:

This is the science-chain benchmark. It should cross external catalog discovery,
data access, format-specific analysis, and visualization without the user naming
NDP, SAC, or plotting internals.

Worked because you saw:

- NDP catalog discovery through `ndp_*` tools.
- A bounded dataset staged or an honest too-large/unavailable result.
- SAC or waveform inspection evidence.
- A PNG artifact when usable waveform data is available.

## 4. NDP Catalog Discovery

Agent: Data Exploration/Search Agent

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

Why this exists:

This isolates the external discovery stage before adding waveform analysis and
plotting pressure.

Worked because you saw:

- `ndp_*` tool calls or an `ndp_catalog` expert handoff.
- Dataset candidates grounded in catalog results.
- Verification cautions before download or staging.

## 5. No-Guard ADIOS/BP5 Route

Agent: Data Exploration/Search Agent

Prompt:

```text
This ADIOS BP5 output came from a Gray-Scott run: "{adios}". Tell me what the container looks like, whether profiling metadata is present, and what extra runtime is needed if variable-level metadata is unavailable.
```

Why this exists:

This catches whether ADIOS/BP5 support is only a suffix guard. The benchmark
expects the orchestrator path to select the right data capability.

Worked because you saw:

- `adios_inspect_file` evidence.
- BP5/profiling metadata discussion.
- Honest dependency caveats if ADIOS2 runtime support is limited.

## Notes For Manual TUI Runs

If a turn visibly routes through a shortcut or returns a plausible answer with no
tool evidence, mark it as failed even if the final text looks reasonable. This
benchmark is about orchestration, tools, and provenance, not just answer shape.
