# Real Provider Semantic Regression

This audit track verifies CLIO/GACT behavior that mocked planner and tool
tests cannot validate. It uses real model calls, real local datasets, real
tool argument generation, and GACT wire events.

## Goal

Build and run a real-provider CLIO/GACT regression harness that exercises the
system the way users hit it: real routing, tool selection, tool arguments,
HDF5/Parquet/CSV ingestion, plotting, tool-result feedback, multi-turn state,
malformed planner output recovery, hidden fallback prevention, streaming
provenance, cancellation truth, and provider/tool error surfacing.

Every observed product failure should be filed before it is fixed. Fixes
should be split one issue per branch/PR, then retested with the same prompts.

## Qwopus Run

Provider:

- LM Studio at `http://127.0.0.1:1234/v1`
- Model: `qwopus3.5-9b-v3`
- `CLIO_LM_MAX_TOKENS=4096`
- `CLIO_LM_PLANNER_MAX_TOKENS=4096`
- `CLIO_LM_PLANNER_TEMPERATURE=0`

Qwopus needs the planner token budget above the default small-model smoke
settings. With `CLIO_LM_MAX_TOKENS=1024`, the model spent too many tokens in
reasoning and the planner failed to emit usable JSON. Keep planner
temperature deterministic (`0`) and use at least `4096` planner tokens for
this regression suite.

Server:

```powershell
$env:CLIO_LM_PROVIDER='lm_studio'
$env:CLIO_LM_MODEL='qwopus3.5-9b-v3'
$env:CLIO_LM_API_BASE='http://127.0.0.1:1234/v1'
$env:CLIO_LM_MAX_TOKENS='4096'
$env:CLIO_LM_PLANNER_MAX_TOKENS='4096'
$env:CLIO_LM_PLANNER_TEMPERATURE='0'
$env:CLIO_ALLOWED_ROOTS='D:\Libraries\Documents\projects\clio-agent'
uv run clio-agent-gact --host 127.0.0.1 --port 17902
```

Suite:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17902'
$env:CLIO_REAL_AUDIT_LOG='D:\Libraries\Documents\projects\clio-agent\.tmp-real-provider\audit-qwopus-expanded.jsonl'
uv run pytest tests/test_integration_contract/test_real_provider_semantics.py -m integration -vv -s
```

Initial result on 2026-05-20 before product fixes:

- `4 passed`
- `5 failed`
- Runtime: `200.23s`

After #245 was fixed and merged, `/v1/providers/lm` was retested on
`develop` with the same Qwopus/LM Studio boot configuration and returned:

```json
{
  "configured": true,
  "provider": "lm_studio",
  "api_base": "http://127.0.0.1:1234/v1",
  "model": "qwopus3.5-9b-v3",
  "max_tokens": 4096,
  "transport": null
}
```

After #247 and #244 were fixed and merged, cancellation and stream provenance
were retested on `develop` with the same prompts and provider settings.

After the #243 planner JSON recovery fix, the full Qwopus suite was retested
with the same provider, datasets, and prompts:

```text
9 passed in 213.73s
```

After #253, #254, and #257 were fixed and merged, the full Qwopus suite was
run again on `develop` (`cced236`) to verify the later Claude/provider and
visualization provenance changes did not regress the local-model path:

```text
9 passed in 174.31s
```

The audit log showed real tool calls for HDF5, Parquet, CSV, and plotting;
structured `tool_error` for a missing file; structured `cancelled` for client
cancellation; and explicit `stream_source=batch` plus structured
fallback reasons when no live deltas were emitted.

## Evidence Matrix

| Case | Provider | Dataset/workflow | Expected behavior | Observed behavior | Status |
| --- | --- | --- | --- | --- | --- |
| `hdf5_tool_loop` | Qwopus / LM Studio | `data/atmospheric.h5` | Route to data, call HDF5 tool, answer with dataset names | Routed `data`, called `hdf5_list_datasets`, answered `simulation/pressure`, `simulation/temperature`, `time_step` | Pass |
| `parquet_tool_loop` | Qwopus / LM Studio | `data/measurements.parquet` | Route to analysis, call Parquet tool, answer with schema columns | Routed `analysis`, called `parquet_analyze_schema`, answered expected columns | Pass |
| `csv_tool_loop` | Qwopus / LM Studio | `data/observations.csv` | Route to analysis, call CSV tool, answer with headers | Routed `analysis`, called `csv_read_table`, answered `sample_id`, `temperature_k`, `pressure_pa` | Fixed: #243 |
| `multiturn_dataset_context_setup` | Qwopus / LM Studio | `data/measurements.parquet` | First turn should inspect data so follow-up can use state | Routed `analysis`, called `parquet_analyze_schema`, then follow-up answered `pressure` | Fixed: #243 |
| `missing_file_tool_error` | Qwopus / LM Studio | Missing HDF5 path | Surface structured tool error without normal answer text | `error_info.error=tool_error`, `tool_error.code=file_not_found`, empty answer text | Pass |
| `visualization_tool_loop` | Qwopus / LM Studio | Parquet summary dashboard | Route to visualization, call plotting tool, return artifact path | Routed `visualization`, called `plot_summary`, wrote `measurements_dashboard.png`, returned `.png` path | Pass |
| `provider_endpoint_effective_config` | Qwopus / LM Studio | `/v1/providers/lm` | Report effective provider/model/api_base | Initially failed with blank provider/model/api_base; retest after #245 returned Qwopus/LM Studio config | Fixed: #245 |
| `cancellation_truth` | Qwopus / LM Studio | Long Parquet turn plus `/cancel` | Settle as structured cancellation and keep polling healthy | `POST /cancel` returned empty 204; final turn surfaced structured `cancelled` | Fixed: #247 |
| `streaming_provenance_truthful` | Qwopus / LM Studio | SSE text turn | Completed event must identify live vs post-hoc provenance | Planner error surfaced without fake answer; completed metadata reported `batch` and fallback reason | Fixed: #244 |

## Filed Issues

- #243: Qwopus planner sometimes rejects recoverable near-JSON actions in real provider runs. Fixed by PR #252.
- #244: GACT `message.completed` can omit stream provenance metadata on real-provider error turns. Fixed by PR #251.
- #245: GACT `/v1/providers/lm` omits boot-time LM provider/model for env-configured agents. Fixed by PR #249.
- #246: CLIO has no Claude Code provider path for real-provider semantic regression runs. Fixed by PR #255.
- #247: Cancelling a live Qwopus GACT turn can break the polling connection with h11 Content-Length error. Fixed by PR #250.
- #253: Claude Code run can route CSV inspection to `fs_read_file` with no owning expert. Fixed by PR #256.
- #254: Claude Code provider fails visualization expert path with LiteLLM async streaming error. Fixed by PR #258.
- #257: Claude Code visualization route can report chart artifact without tool telemetry. Fixed by PR #259.

## Claude Status

Claude Code is installed and authenticated locally:

```powershell
claude --version
# 2.1.145 (Claude Code)

claude -p --output-format json --model sonnet "Return exactly CLAUDE_CODE_OK and nothing else."
# result: CLAUDE_CODE_OK
```

CLIO now exposes a Claude Code provider path through
`CLIO_LM_PROVIDER=claude_code`. It routes through the local `claude -p`
CLI as a LiteLLM `CustomLLM` and disables Claude Code tools so CLIO's own
planner/tool loop remains authoritative.

Direct Anthropic API remains available through `CLIO_LM_PROVIDER=anthropic`
when `ANTHROPIC_API_KEY` is set.

Claude Code provider smoke:

```powershell
$env:CLIO_LM_PROVIDER='claude_code'
$env:CLIO_LM_MODEL='sonnet'
$env:CLIO_CLAUDE_CODE_TRANSPORT='exec'
```

Direct DSPy smoke returned `CLIO_CLAUDE_CODE_OK`.

GACT suite result on 2026-05-20:

```text
7 passed, 2 failed in 161.10s
```

Passing Claude Code cases:

- HDF5: routed `data`, called `hdf5_list_datasets`.
- Parquet: routed `analysis`, called `parquet_analyze_schema`.
- Multi-turn: first Parquet turn called `parquet_analyze_schema`; follow-up answered `pressure`.
- Missing file: surfaced structured `tool_error`.
- Provider endpoint: reported `claude_code/sonnet`.
- Cancellation: surfaced structured `cancelled`.
- Streaming provenance: emitted explicit `batch` provenance.

Filed Claude Code failures:

- #253: CSV prompt selected `fs_read_file`, a planner-visible tool with no registered owning expert. Fixed by PR #256; focused live retest selected `analysis` and called `csv_read_table`.
- #254: visualization expert path hit LiteLLM async/streaming incompatibility in the Claude Code custom provider. Fixed by PR #258; Claude Code is marked non-live-streaming and GACT records `provider_streaming_unsupported`.
- #257: after #254's streaming bypass, the same visualization prompt selected `visualization` and produced a `.png` artifact path, but GACT reported no `plot_` tool telemetry. Fixed by PR #259; focused live retest reported `plot_summary` with `telemetry_source=live_observer`.

After #253, #254, and #257 were fixed and merged, the full Claude Code suite
was rerun on `develop` (`cced236`) with the same datasets and prompts:

```text
9 passed in 146.12s
```

Final Claude Code audit rows showed:

- HDF5: `selected_agent=data`, `tools_called=hdf5_list_datasets`.
- Parquet: `selected_agent=analysis`, `tools_called=parquet_analyze_schema`.
- CSV: `selected_agent=analysis`, `tools_called=csv_read_table`.
- Multi-turn: first turn called `parquet_analyze_schema`; follow-up answered from session context.
- Missing file: structured `error_info.error=tool_error`.
- Visualization: `selected_agent=visualization`, `tools_called=plot_summary`.
- Provider endpoint: reported `claude_code/sonnet`.
- Cancellation: structured `error_info.error=cancelled`.
- Streaming provenance: `stream_source=batch` with explicit fallback metadata.

## Remaining Work

- No open failures remain from the real-provider semantic matrix. Remaining
  caveat: CLI-backed providers (`claude_code`, `codex`) are non-live-streaming;
  GACT reports completed text as `batch` with
  `provider_streaming_unsupported` rather than fake live token deltas.
