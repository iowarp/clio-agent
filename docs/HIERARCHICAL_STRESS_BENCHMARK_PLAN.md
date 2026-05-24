# CLIO Hierarchical Stress Benchmark Plan

The current ALCF demo benchmark is a useful smoke/demo suite. It is not the
target benchmark for CLIO.

CLIO's core claim is hierarchical intelligence: the system should acquire
capability by delegating work to scoped experts, preserving each expert's
context, composing discoveries across experts, and exposing every tool call,
handoff, error, and artifact to the user. Benchmarks must therefore stress
hierarchy, not just provider connectivity.

## Benchmark Standard

A benchmark case is not considered a full CLIO benchmark unless it exercises
most of these properties:

- Natural user prompt with minimal route/tool spelling.
- Orchestrator chooses a workflow over multiple expert scopes.
- At least two expert contexts are used, except for explicit single-domain
  stress tests.
- Tool ownership is respected; experts do not see or call unrelated tools.
- Tier-3 agents or nanoagents are spawned when the task has independent
  subtasks.
- One expert's result becomes another expert's input without user rewriting.
- Tool calls and results are visible in the GACT transcript.
- Errors surface as errors, not generic fallback text or repeated canned
  assistant prose.
- The run produces persistent evidence: prompt, route, expert handoffs, tool
  calls, child sessions, artifacts, timings, error info, and final answer.

Small tests are still useful when they deliberately stress a failure boundary:
permission gates, context overflow, large files, cancellation, provider swaps,
tool errors, malformed model output, missing resources, or transcript replay.

## Target Workflows

### 1. NDP Seismic Discovery To Plot

Prompt shape:

> Find seismic data from a seismological organization on NDP. Pick a usable
> dataset, inspect the data, analyze the signal across three axes, and produce
> a plot with the three-axis trace.

Expected hierarchy:

```text
orchestrator
  -> data
      -> ndp_catalog
          -> search/list/filter candidate datasets
          -> inspect metadata/resources
      -> data_access
          -> download or stage selected resource
          -> identify format, schema, variables, coordinates, units
  -> analysis
      -> compute signal summaries and validate axis semantics
      -> produce structured analysis insights
  -> visualization
      -> consume analysis result plus data references
      -> generate plot artifact
```

Pass criteria:

- NDP calls are owned by `data` or a nested `ndp_catalog` agent, not by
  `analysis`.
- Candidate dataset choice is explained from metadata.
- Download/staging either succeeds or fails with a concrete surfaced error.
- Analysis receives structured data/context from the data stage.
- Visualization receives analysis output and produces an artifact.
- The transcript shows the handoff chain and tool evidence.

Current implementation evidence:

- NDP discovery is owned by `data`, not `analysis`.
- Data-stage discovery now calls `ndp_get_dataset_details` before staging.
- `ndp_stage_resource` can stage bounded HTTP(S) resources under CLIO file
  policy. For OSDF/Pelican resources it checks advertised size before invoking
  Pelican, uses the local `pelican` CLI when available, and surfaces concrete
  `resource_too_large`, `pelican_unavailable`, or `pelican_stage_failed` errors.
- The live Salton Sea seismic candidate currently exposes an `osdf://` resource.
  It is advertised as about 1.4 GB, so CLIO reports `resource_too_large` with
  the default staging cap and keeps analysis/plotting blocked until a smaller
  concrete object is selected or the dataset is intentionally staged manually.
  This is correct failure surfacing, not a completed plot benchmark.
- A follow-up ALCF/GACT run found a bounded HIVE SAC waveform archive from NDP,
  staged `Pachhai_etal_2023_ScP_data.tar`, inspected 11260 SAC traces, computed
  representative trace statistics, and produced a PNG plot with three traces.
  The live route selected `visualization` after data-owned NDP discovery and
  automatic data -> analysis -> visualization handoff. Tool evidence included
  `ndp_list_organizations`, three `ndp_search_datasets` calls,
  `ndp_get_dataset_details`, `ndp_stage_resource`, `sac_inspect_archive`,
  `sac_compute_trace_statistics`, and `sac_plot_traces`.
- The format tool surface is deliberately SAC-specific. It is exposed as a
  `sac` FastMCP server with `sac_*` tools, not as a generic seismic namespace.
- Caveat: the completed staged waveform demo is SAC archive based. The original
  Salton Sea three-component MiniSEED path remains a future target because the
  discovered OSDF resource is large and requires a bounded Pelican/object
  selection path.
- Architecture caveat: this implementation proves data-owned NDP discovery, but
  NDP semantics still live inside the top-level DataExpert. The intended CLIO
  hierarchy is `data -> ndp_catalog` or `data -> ndp_access`, where the nested
  NDP expert owns NDP-specific prompt context, tools, dataset/resource ranking,
  and eventually its own tuned model. Future benchmarks should include
  EarthScope-oriented prompts and verify that NDP work is delegated to that
  nested expert rather than handled directly by DataExpert.

### 2. Mixed Scientific Run Audit

Prompt shape:

> I have an HDF5 file, a BP5 run directory, a Parquet measurement table, and a
> CSV event log from the same experiment. Check whether they look like the same
> run, identify inconsistencies, and produce a concise summary plus one plot
> that would help a beamline scientist decide whether to trust the run.

Expected stress:

- Data expert fans out to format-specific workers.
- ADIOS/HDF5/Parquet/CSV tools run in parallel where possible.
- Analysis synthesizes cross-file consistency and quality concerns.
- Visualization produces a plot from the most relevant tabular signal.
- At least one child worker should fail gracefully if a file is malformed.

### 3. Dirty Real-World Tabular Quality Review

Prompt shape:

> This data table came from a collaborator and may be messy. Find schema
> problems, suspicious columns, null patterns, outliers, and units that do not
> make sense. Do not assume the column names are reliable.

Expected stress:

- Multiple statistics/query calls.
- Robustness to nulls, strings in numeric columns, bad timestamps, duplicate
  rows, and mixed units.
- Analysis should produce concrete evidence, not generic quality advice.
- Visualization may produce a missingness or outlier plot if useful.

### 4. Context Pressure And Compaction

Prompt shape:

> Review these many files and keep a running summary. After each batch, tell me
> what evidence matters and continue without losing earlier conclusions.

Expected stress:

- Enough tool output and conversation state to force context compression.
- The system should preserve decisions, artifacts, and unresolved questions.
- No repeated stale answers after compaction.
- Tool visibility and expert context should remain scoped after compaction.

### 5. Large File And Memory Safety

Prompt shape:

> Inspect this very large file and give me useful statistics without loading
> the whole thing into memory.

Expected stress:

- Tools must stream, sample, push down row limits, or fail clearly.
- No full-table reads hidden behind friendly text.
- Memory/time limits are reported as first-class errors.

### 6. Provider And Model Swap During Work

Prompt shape:

> Start a multi-step analysis, then switch providers/models before the follow-up
> question and verify the session remains coherent.

Expected stress:

- Provider/model swap marker appears in the transcript.
- The active session does not retain stale model refs.
- Follow-up answers use preserved conversation/tool context.
- Provider errors do not collapse into `(no parts)` or silent retries.

### 7. Tool Ownership And Capability Boundaries

Prompt shape:

> Ask each expert what it can do, then ask for a task that tempts the wrong
> tool boundary.

Expected stress:

- Chat sees only chat-visible utility tools.
- Data owns file/data discovery tools.
- Analysis owns quantitative reasoning over known data.
- Visualization owns plotting tools.
- NDP is nested under data/discovery, not analysis.
- Wrong-boundary tool calls are rejected with structured errors.

### 8. Skills And User-Agent Extension

Prompt shape:

> Add or select a skill/agent that knows a domain-specific workflow, then use it
> inside a larger CLIO task without editing core routing code.

Expected stress:

- Skills appear as selectable agents with details.
- The orchestrator can route to newly registered agents dynamically.
- Tools/skills exposed to that agent are scoped by metadata, not hardcoded
  central lists.

## Failure-Hunting Cases

These are intentionally adversarial and may be shorter than the workflows
above:

- Nine or more tool calls in one turn.
- Multiple nanoagents running in parallel with one failing.
- Missing file, corrupt file, unsupported format, and empty dataset.
- Overlarge tool output requiring truncation/expansion in the TUI.
- Repeated cancellation during long external calls.
- Provider timeout during a child-agent workflow.
- Malformed planner JSON from local models.
- Tool returns success-shaped garbage.
- Session delete/reload during active or recently completed tool calls.
- Unicode paths, non-Latin column names, and localized TUI strings in outputs.

## Evidence Required For Each Run

Every benchmark run should save:

- Prompt and scenario ID.
- Provider/model/context settings.
- Route decision and route source.
- Expert handoff graph.
- Per-expert context summary.
- Tool calls with arguments, results, errors, and duration.
- Child/nanoagent sessions and their status.
- Artifacts created and file paths.
- Token usage when available.
- Wall-clock time.
- Final answer.
- Pass/fail reason.
- Bugs filed or fixes made.

## Completion Criteria

A benchmark campaign is complete only when:

- At least ten complex demos are documented for humans to run.
- At least five cases run longer than two minutes or include more than ten
  visible tool/handoff events.
- At least three cases include tier-3 agents or nanoagents.
- At least three cases produce visualization artifacts from analyzed data.
- At least two cases deliberately trigger and surface errors.
- At least one case forces context pressure or compaction.
- All discovered issues are logged before or while being fixed.
- The final report distinguishes smoke/demo coverage from true stress coverage.
