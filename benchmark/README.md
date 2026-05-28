# CLIO Scientific Benchmark

This directory contains the reusable benchmark prompt book for CLIO's real
provider scientific workflow runs.

The benchmark is not an ALCF-only demo. ALCF Metis/Sophia runs are one evidence
lane, alongside local LM Studio/Qwopus and future provider lanes. The benchmark
contract is the prompt family, expected routing/tool behavior, artifacts, memory
semantics, and surfaced-error behavior.

## Files

- `REAL_PROVIDER_PROMPTS.md` - collaborator-grade prompts and expected behavior.
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
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl tmp/clio-real-provider-benchmark.jsonl \
  --report docs/ALCF_DEMO_BENCHMARK_REPORT.md
```

The `--report` path is still the historical default report target. New evidence
can use a provider-specific path under `docs/` or a future report path under
this `benchmark/` directory.
