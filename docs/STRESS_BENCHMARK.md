# CLIO Real-Provider Benchmark And Demo Guide

This guide is for running CLIO in front of external collaborators with a real
local model and real scientific files. It is not a mock demo. The prompts below
exercise routing, tool calling, multi-agent delegation, nano-agent fan-out,
memory, visualization, streaming provenance, cancellation, and error surfacing.

The baseline path is local-first: LM Studio serving Qwopus through the
OpenAI-compatible API. ALCF is optional and useful for provider comparison after
the local path is stable.

## 1. Install And Prepare

From the repository root:

```powershell
uv sync --extra dev --extra optimizers
uv run python scripts/create_benchmark_data.py --output-dir tmp/clio-benchmark-data
```

The data generator creates a deterministic benchmark bundle:

- `tmp/clio-benchmark-data/fusion_run.h5`
  Multi-group fusion-style HDF5 with plasma temperature, density, heat flux,
  quality flags, compression, chunks, and dataset attributes.
- `tmp/clio-benchmark-data/facility_measurements.parquet`
  3,000-row Parquet file with row groups, numeric sensor columns, categorical
  fields, quality flags, and metadata.
- `tmp/clio-benchmark-data/facility_measurements_dirty.parquet`
  A companion Parquet file with missing values and dirtier quality conditions.
- `tmp/clio-benchmark-data/sensor_events.csv`
  420-row event stream with timestamps, sensor values, status, and operator
  notes.

## 2. Configure LM Studio

Install LM Studio and load:

```text
Jackrong/Qwopus3.5-9B-v3-GGUF
```

Use the model name exposed by LM Studio. In the benchmark runs here it was:

```text
qwopus3.5-9b-v3
```

Recommended LM Studio settings for a 16 GB GPU class machine:

- Backend: ROCm on AMD GPUs when available; Vulkan is a fallback.
- Context length: `32768` for benchmark/demo work.
- GPU offload: as much as LM Studio can fit stably.
- Keep model in memory: enabled.
- Flash attention: enabled if LM Studio allows it for the loaded backend.
- Temperature in CLIO: `0`.
- CLIO max tokens: `8192`.

The `8192` answer budget matters. Earlier `4096` runs completed tool calls but
could run out of output budget during local-model synthesis, producing a
structured routing error instead of a final answer.

## 3. Start CLIO/GACT

In PowerShell from the repo root:

```powershell
$repo = (Resolve-Path '.').Path
$data = (Resolve-Path 'tmp/clio-benchmark-data').Path

$env:CLIO_LM_PROVIDER = 'lm_studio'
$env:CLIO_LM_MODEL = 'qwopus3.5-9b-v3'
$env:CLIO_LM_API_BASE = 'http://127.0.0.1:1234/v1'
$env:CLIO_LM_MAX_TOKENS = '8192'
$env:CLIO_LM_TEMPERATURE = '0'
$env:CLIO_LM_PLANNER_TEMPERATURE = '0'
$env:CLIO_ALLOWED_ROOTS = "$repo;$data"
$env:CLIO_DATA_DIR = "$repo/tmp/clio-demo-state"

uv run clio-agent-gact --host 127.0.0.1 --port 17910
```

Confirm the server is live:

```powershell
Invoke-RestMethod http://127.0.0.1:17910/v1/health
```

Then use the GACT TUI against `http://127.0.0.1:17910`, or post prompts through
the API. The TUI is better for demos because collaborators can see tool
metadata, streaming, child sessions, and artifacts.

## 4. Demo Prompt Book

Before the demo, set path variables so prompts can be copied accurately:

```powershell
$data = Resolve-Path tmp/clio-benchmark-data
$h5 = Join-Path $data 'fusion_run.h5'
$parquet = Join-Path $data 'facility_measurements.parquet'
$dirty = Join-Path $data 'facility_measurements_dirty.parquet'
$csv = Join-Path $data 'sensor_events.csv'
```

Replace `$h5`, `$parquet`, `$dirty`, and `$csv` in the prompts below with the
resolved full paths if you are typing directly into the TUI.

### Tooling: Inspect A Scientific HDF5 File

Prompt:

```text
Use CLIO tools to inspect this HDF5 benchmark file. File: $h5. Report the dataset names, shapes, compression, and units where available. Also explain which datasets look most important for downstream analysis.
```

What you should see:

CLIO should route to the data expert and call HDF5 tools such as
`hdf5_analyze_file`, `hdf5_list_datasets`, and `hdf5_check_compression`. The
answer should mention datasets like `plasma/electron_temperature`,
`plasma/density`, `diagnostics/heat_flux`, and `quality/flags`, plus shapes and
compression information. Tool calls should be visible in message metadata.

Why this is interesting:

This is the cleanest proof that the model is not just talking about HDF5. It has
to generate a real file path argument, call deterministic tools, read structured
results, and summarize domain-relevant file structure.

### Multi-Agent Workflow: Data, Analysis, CSV, Visualization

Run these prompts in the same session, in order.

Prompt 1:

```text
Use CLIO tools to inspect this HDF5 benchmark file. File: $h5. Report the dataset names, shapes, compression, and units where available.
```

Prompt 2:

```text
Use CLIO tools to profile this Parquet benchmark file. File: $parquet. Include schema, row groups, and statistics for temperature_k, pressure_pa, humidity_pct, and anomaly_score.
```

Prompt 3:

```text
Use CLIO tools to inspect this CSV benchmark event stream. File: $csv. List the columns and identify the status and operator_note fields.
```

Prompt 4:

```text
Using the Parquet benchmark file we just profiled, use CLIO visualization tools to create a summary dashboard for $parquet. Return the PNG artifact path and explain what the chart is summarizing.
```

What you should see:

The selected expert should change by task: data for HDF5, analysis for Parquet
and CSV, visualization for the chart. The final turn should produce a `.png`
artifact path that exists on disk. The tool list should include HDF5, Parquet,
CSV, and `plot_summary` calls across the conversation.

Why this is interesting:

This demonstrates CLIO as a workflow system rather than a single chatbot turn.
The model has to choose different experts, preserve enough context to understand
"the file we just profiled", use tools with correct arguments, and produce a
real artifact.

### Nano-Agents: Parallel File Validation

Prompt:

```text
Validate in parallel: HDF5 structure for $h5, Parquet statistics for $parquet, and CSV schema for $csv. Spawn nanoagents for the independent checks and use CLIO tools in each worker. Summarize all worker findings in the parent answer.
```

What you should see:

CLIO should create child sessions for workers such as `data_validator`,
`analysis_validator`, and `csv_validator`. The parent answer should summarize
all three workers. Child assistant messages should include real tool provenance:
HDF5 tools in the HDF5 worker, Parquet tools in the Parquet worker, and
`csv_read_table` in the CSV worker.

Why this is interesting:

This is a high-value stress case. It checks that nano-agents are not just
prompt-only summaries. Each worker must run real CLIO tools, and the parent
turn must aggregate the independent findings.

### Memory: Follow-Up Without Repeating The Path

Run this after the Parquet profiling prompt in the same session.

Prompt:

```text
Based on the Parquet file we just profiled, which fields would you use for a quick anomaly triage view? Do not ask me for the path again; use the current session context.
```

What you should see:

CLIO should use the prior session context to stay grounded in
`facility_measurements.parquet`. A good answer should refer to columns such as
`temperature_k`, `pressure_pa`, `humidity_pct`, `vibration_mm_s`,
`anomaly_score`, `quality_flag`, and `valid`. If the planner decides to use
tools again, the tool arguments should still point to the same Parquet file.

Why this is interesting:

This demonstrates memory and context reuse. The user does not restate the file
path, so CLIO has to use session state instead of hallucinating a new dataset or
asking a needless clarification.

### Error Surface: Missing File

Prompt:

```text
Use CLIO tools to inspect this missing HDF5 benchmark file and report the datasets: tmp/clio-benchmark-data/missing_fusion_run.h5
```

What you should see:

The turn should surface structured `error_info`, usually a `tool_error`, and the
assistant should not produce a normal-looking fake dataset summary. The error is
allowed to be recoverable, but it must be explicit.

Why this is interesting:

This proves the failure contract. CLIO should not hide provider or tool failures
behind canned text, repeated previous answers, or plausible invented results.

### Streaming Provenance: Live Or Batch Truth

Create a session with `routing_mode="chat"` for this prompt, then ask:

```text
In 180 words, explain why scientific workflow agents need evidence logs for routing, tool calls, artifacts, and failures.
```

What you should see:

The response should complete without `error_info`. Streaming metadata should
tell the truth: `stream_source="live"` when visible deltas were emitted, or
`stream_source="batch"` with a fallback reason when the provider/DSPy path only
returned a completed answer.

Why this is interesting:

This separates real streaming from post-hoc display. For collaborators, the key
point is not that every provider streams live; it is that CLIO labels what
happened accurately.

## 5. Run The Repeatable Benchmark Suite

The manual prompts are for demos. The repeatable gate is pytest:

```powershell
$env:CLIO_INTEGRATION_BASE = 'http://127.0.0.1:17910'
$env:CLIO_BENCHMARK_DATA_DIR = "$(Resolve-Path tmp/clio-benchmark-data)"
$env:CLIO_STRESS_AUDIT_LOG = "$(Resolve-Path tmp)\clio-stress-audit.jsonl"

uv run pytest tests/test_stress_benchmark -m "integration and benchmark" -vv -s
```

Current local Qwopus evidence from this branch:

```text
5 passed in 334.37s
```

The suite covers:

- multi-turn HDF5, Parquet, CSV, and visualization workflow
- tool-backed nano-agent fan-out and parent aggregation
- missing-file error surfacing with no fake answer
- cancellation surfacing as structured cancellation
- streaming provenance with live-or-batch truth

The audit log is JSONL. Each row records provider/model, prompt, dataset,
selected expert, tools called, artifacts, child sessions, error info, stream
metadata, answer excerpt, and caveats.

## 6. Reading Demo Results

For a healthy demo, look for:

- `selected_agent`: matches the task domain (`data`, `analysis`,
  `visualization`, or `chat`).
- `tools_called`: includes the real tools expected for the file type.
- `metadata.tools_called[*].telemetry_source`: `live_observer` for live
  lifecycle telemetry, `agent_trace` for real agent trace rows, or
  `posthoc_prediction` only for genuine post-hoc prediction metadata.
- `error_info`: absent on successful turns; present and explicit on failure or
  cancellation turns.
- `stream_source`: `live` or `batch`, never ambiguous.
- Artifacts: PNG paths should exist when visualization is requested.
- Nano-agent child sessions: child assistant messages should contain
  `metadata.tools_called`.

Partial routing errors can still appear in successful local-model tool turns,
for example `stage="post_observation_planning"` or
`stage="parallel_validation_recovery"`. Those mean CLIO completed useful tool
work and surfaced the local planner's imperfect JSON behavior instead of
pretending nothing happened.

## 7. Optional ALCF Provider Lane

ALCF is not the baseline for local hardening. Use it after the local Qwopus demo
works, mainly to compare provider semantics.

Install the optional dependency and authenticate:

```powershell
uv sync --extra dev --extra optimizers --extra argonne
uv run --extra argonne python -m clio_agent.providers.argonne_auth authenticate
```

The auth helper requires an interactive terminal. It prints a Globus URL and
expects the authorization code to be pasted into the same terminal. A successful
run ends with:

```text
Authentication complete.
```

Discover models through the helper, not through `/models`:

```powershell
uv run --extra argonne python scripts/list_alcf_models.py
```

Then configure CLIO, for example:

```powershell
$env:CLIO_LM_PROVIDER = 'argonne'
$env:CLIO_LM_API_BASE = 'https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1'
$env:CLIO_LM_MODEL = 'meta-llama/Llama-4-Scout-17B-16E-Instruct'
$env:CLIO_LM_MAX_TOKENS = '4096'
$env:CLIO_LM_PLANNER_TEMPERATURE = '0'
```

Model availability is volatile. Re-run discovery before benchmark work.
