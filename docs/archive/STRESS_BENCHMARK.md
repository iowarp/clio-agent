# CLIO Real-Provider Benchmark And Demo Guide

This guide is for running CLIO in front of external collaborators with a real
local model and real scientific files. It is not a mock demo. The prompts below
exercise routing, tool calling, multi-agent delegation, nano-agent fan-out,
memory, visualization, streaming provenance, cancellation, and error surfacing.
The main prompts are intentionally human-natural: they do not ask for specific
experts, tool names, or nano-agents unless the section is explaining what to
inspect in the evidence.

For the current verified evidence matrix and known gaps, see
`docs/STRESS_BENCHMARK_REPORT.md`.

The baseline path is local-first: LM Studio serving Qwopus through the
OpenAI-compatible API. ALCF is optional and useful for provider comparison after
the local path is stable.

For the current ALCF/gpt-oss demo matrix, see
`docs/ALCF_DEMO_BENCHMARK_REPORT.md`. That report is generated from real GACT
turns and includes the best 10 collaborator-ready prompts plus observed tools,
child sessions, artifacts, route sources, and failures when they occur.

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
- `tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5`
  Real ADIOS/BP5 Gray-Scott output when the adjacent BP5 dataset collection is
  available, otherwise a BP-like container with profiling metadata. CLIO can
  inspect the container and profiling metadata without ADIOS2. Variable-level
  metadata requires the optional ADIOS2 Python bindings.

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

## 3a. Optional ALCF/gpt-oss Demo Runner

When a valid ALCF Globus token is available, the larger hosted models are useful
for a stronger semantic demo pass. Start a GACT backend with Sophia/gpt-oss:

```powershell
$repo = (Resolve-Path '.').Path
$data = (Resolve-Path 'tmp/clio-benchmark-data').Path

$env:CLIO_LM_PROVIDER = 'argonne'
$env:CLIO_LM_API_BASE = 'https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1'
$env:CLIO_LM_MODEL = 'openai/gpt-oss-120b'
$env:CLIO_LM_MAX_TOKENS = '8192'
$env:CLIO_LM_PLANNER_MAX_TOKENS = '4096'
$env:CLIO_LM_TEMPERATURE = '0'
$env:CLIO_LM_PLANNER_TEMPERATURE = '0'
$env:CLIO_GACT_TURN_TIMEOUT_S = '900'
$env:CLIO_ALLOWED_ROOTS = "$repo;$data"
$env:CLIO_DATA_DIR = "$repo/tmp/clio-alcf-demo-state"

uv run clio-agent-gact --host 127.0.0.1 --port 17960
```

In another terminal, run the demo benchmark:

```powershell
uv run python scripts/run_demo_benchmark.py `
  --base-url http://127.0.0.1:17960 `
  --data-dir tmp/clio-benchmark-data `
  --output-jsonl tmp/clio-demo-benchmark-alcf-gptoss120b.jsonl `
  --report docs/ALCF_DEMO_BENCHMARK_REPORT.md
```

The runner covers 14 natural prompts: HDF5 overview, Parquet profiling, memory
follow-up, CSV schema, visualization artifact generation, HDF5 dataset deep
dive, guard and no-guard cross-file nano-agent triage, guard and no-guard
ADIOS/BP5 inspection, dirty Parquet quality review, NDP catalog discovery,
targeted scatter plotting, and missing-file error surfacing.

## 4. Demo Prompt Book

Before the demo, set path variables so prompts can be copied accurately:

```powershell
$data = Resolve-Path tmp/clio-benchmark-data
$h5 = Join-Path $data 'fusion_run.h5'
$parquet = Join-Path $data 'facility_measurements.parquet'
$dirty = Join-Path $data 'facility_measurements_dirty.parquet'
$csv = Join-Path $data 'sensor_events.csv'
$adios = Join-Path $data 'gray scott noise 0.01 data.bp5'
```

Replace `$h5`, `$parquet`, `$dirty`, `$csv`, and `$adios` in the prompts below
with the resolved full paths if you are typing directly into the TUI.

### Tooling: Inspect A Scientific HDF5 File

Prompt:

```text
I need to understand this fusion run before sharing it. File: $h5. What datasets are inside, what are their shapes, and what units or compression details should I know about? Also explain which datasets look most important for downstream analysis.
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
I need to understand this fusion run before sharing it. File: $h5. What datasets are inside, what are their shapes, and what units or compression details should I know about?
```

Prompt 2:

```text
Profile the facility measurements in this Parquet file: $parquet. I care about the schema, row groups, and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane.
```

Prompt 3:

```text
This event stream came with the run: $csv. What columns does it contain, and where are the status and operator_note fields?
```

Prompt 4:

```text
Create a compact PNG dashboard from the Parquet file we just profiled. Tell me where it was saved and what the chart is summarizing.
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

### ADIOS/BP5: Container And Profiling Inspection

Prompt:

```text
This ADIOS BP5 output came from a Gray-Scott run: "$adios". Tell me what the container looks like, whether profiling metadata is present, and what extra runtime is needed if variable-level metadata is unavailable.
```

What you should see:

CLIO should route to the data expert and call `adios_inspect_file`. The answer
should mention BP5 container members such as `data.0`, metadata/index members,
and `profiling.json` when present. On Windows without ADIOS2 installed, CLIO
should explicitly say that ADIOS2 Python bindings are needed for variable-level
metadata rather than pretending the variables were read.

Why this is interesting:

This adds a real HPC format to the demo without hiding platform limitations. It
proves BP5 path extraction, spaces in paths, container/profiling inspection, and
structured dependency surfacing for the ADIOS2 runtime.

### Nano-Agents: Natural Cross-File Triage

Prompt:

```text
I have four related files from the same experiment: $h5, $parquet, $csv, and "$adios". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What you should see:

CLIO should create child sessions for workers such as `data_validator`,
`analysis_validator`, `csv_validator`, and `adios_validator`. The parent answer
should summarize all four workers. Child assistant messages should include real
tool provenance: HDF5 tools in the HDF5 worker, Parquet tools in the Parquet
worker, `csv_read_table` in the CSV worker, and `adios_inspect_file` in the BP5
worker.

Why this is interesting:

This is a high-value stress case. It checks that nano-agents are not just
prompt-only summaries. The user does not name nano-agents or tools; CLIO has to
infer the decomposition from the natural cross-file request. Each worker must
run real CLIO tools, and the parent turn must aggregate the independent
findings.

### Dirty Data: Quality Triage

Prompt:

```text
This Parquet export looks suspicious: $dirty. Review it for data quality problems and tell me what fields need attention before downstream analysis.
```

What you should see:

CLIO should route to the analysis expert, call Parquet schema/statistics tools,
and ground the answer in the dirty file's actual columns and null counts. It
should not produce generic data-cleaning advice without tool evidence.

Why this is interesting:

This catches a different class of failure from the clean demo files: local
models often degrade paths or make generic quality claims. A good run repairs
path formatting when needed, reads the dirty Parquet file, and names concrete
fields such as `temperature_k`, `pressure_pa`, and `quality_flag`.

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

### Core CLIO Path: NDP Catalog Discovery

Prompt:

```text
I need live catalog evidence for external collaborators. Find NOAA climate-related datasets in the National Data Platform catalog, include the organization evidence you used, and mention any resource formats that look useful for downstream analysis.
```

What you should see:

CLIO should route through the core `data` path and call gateway-visible `ndp_`
tools backed by clio-kit. NDP discovery is a data-stage responsibility: the
data expert or a nested catalog specialist should find candidate resources,
then analysis should consume staged data later. A good answer should mention the
National Data Platform, NOAA-related catalog evidence, dataset titles, and
useful downstream formats such as CSV, GEOJSON, ESRI REST, HTML, or KML.
For prompts that ask CLIO to inspect or analyze an NDP resource, the data stage
should attempt `ndp_get_dataset_details` and `ndp_stage_resource`; if the
resource uses an unsupported transport such as OSDF/Pelican, that blocker should
surface as a structured error rather than a made-up analysis result.

Why this is interesting:

This is the path collaborators normally care about: the user asks a natural
catalog question, and CLIO reaches clio-kit/NDP through its own planner/tool
surface without sending catalog discovery to the analysis expert. Older
benchmark runs recorded NDP under analysis; that is now treated as an ownership
bug, not the desired architecture.

### Direct External MCP: CLIO Kit NDP Discovery

Use the GACT MCP server panel or API to install the clio-kit NDP server:

```json
{
  "name": "clio-kit-ndp",
  "transport": "stdio",
  "command": "uv",
  "args": ["--directory", "../clio-kit", "run", "clio-kit", "mcp-server", "ndp"]
}
```

Then call `list_organizations` with:

```json
{"name_filter": "noaa", "server": "global"}
```

If `../clio-kit` is not available, use `uvx` instead:

```json
{
  "name": "clio-kit-ndp",
  "transport": "stdio",
  "command": "uvx",
  "args": ["clio-kit", "mcp-server", "ndp"]
}
```

What you should see:

The server should install with tools `list_organizations`, `search_datasets`,
and `get_dataset_details`. The `list_organizations` call should return real NDP
catalog organizations containing `noaa`, such as NOAA Global Systems Laboratory
or NOAA NCEI rows, with `_meta.status="success"`.

Why this is interesting:

This proves CLIO/GACT can connect to an arbitrary external clio-kit MCP server
and execute a real National Data Platform tool call through the same MCP
install/call surface used by the TUI. It is separate from the core CLIO
planner/tool path above.

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
7 passed in 201.46s
clio-kit NDP core CLIO path: 1 passed in 140.09s
clio-kit NDP focused lane: 1 passed in 4.24s
```

The suite covers:

- multi-turn HDF5, Parquet, CSV, and visualization workflow
- natural cross-file prompt decomposition into tool-backed nano-agent workers
- ADIOS/BP5 container and profiling inspection, with explicit ADIOS2 dependency
  surfacing for variable metadata on Windows
- clio-kit NDP gateway tools in the core CLIO planner/expert path
- clio-kit NDP external MCP install and real `list_organizations` tool call
- dirty Parquet quality review grounded in real schema/statistics tools
- missing-file error surfacing with no fake answer
- cancellation surfacing as structured cancellation
- streaming provenance with live-or-batch truth

The audit log is JSONL. Each row records provider/model, prompt, dataset,
selected expert, routing decision, elapsed runtime, tools called, artifacts,
child sessions, error info, stream metadata, answer excerpt, and caveats.

Planner routing now runs without deterministic pre-planner guards by default.
To opt into registry-declared production guard routing for a guard-specific
comparison run, restart GACT with:

```powershell
$env:CLIO_ROUTING_GUARDS = '1'
$env:CLIO_LM_PLANNER_MAX_TOKENS = '4096'
```

Default benchmark mode is slower and more diagnostic because it tests the hard
planner/orchestrator path. In audit rows, `route_source="guard"` means a
registry-declared guard selected the route before the planner; `route_source="dspy"`
means the planner selected it; `route_source="recovery"` means the planner failed
and CLIO recovered through a deterministic fallback while surfacing `error_info`.

Do not force Qwopus below a `4096` planner cap. CLIO now raises too-small
Qwopus/Qwen planner caps to `4096` because lower caps repeatedly cut off valid
planner output and produce structured `routing_error` turns.

## 6. Reading Demo Results

For a healthy demo, look for:

- `selected_agent`: matches the task domain (`data`, `analysis`,
  `visualization`, or `chat`).
- `routing_decision.metadata.route_source`: `dspy` for planner-selected routes,
  `guard` for deterministic pre-planner routing, or `recovery` for fallback
  after planner failure.
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
$env:CLIO_LM_MODEL = 'openai/gpt-oss-20b'
$env:CLIO_LM_MAX_TOKENS = '4096'
$env:CLIO_LM_PLANNER_TEMPERATURE = '0'
```

Model availability is volatile. Re-run discovery before benchmark work.
Prefer currently active modern models such as `openai/gpt-oss-20b`,
`openai/gpt-oss-120b`, or Llama 4 variants over older Llama 3.1 models when
they are available.
