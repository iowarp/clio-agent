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
uv run pytest tests/test_integration_v0_2/test_real_provider_semantics.py -m integration -vv -s
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

The audit log showed real tool calls for HDF5, Parquet, CSV, and plotting;
structured `tool_error` for a missing file; structured `cancelled` for client
cancellation; and explicit `stream_source=synthetic_posthoc` plus structured
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
| `streaming_provenance_truthful` | Qwopus / LM Studio | SSE text turn | Completed event must identify live vs post-hoc provenance | Planner error surfaced without fake answer; completed metadata reported `synthetic_posthoc` and fallback reason | Fixed: #244 |

## Filed Issues

- #243: Qwopus planner sometimes rejects recoverable near-JSON actions in real provider runs. Fixed by PR #252.
- #244: GACT `message.completed` can omit stream provenance metadata on real-provider error turns. Fixed by PR #251.
- #245: GACT `/v1/providers/lm` omits boot-time LM provider/model for env-configured agents. Fixed by PR #249.
- #246: CLIO has no Claude Code provider path for real-provider semantic regression runs.
- #247: Cancelling a live Qwopus GACT turn can break the polling connection with h11 Content-Length error. Fixed by PR #250.

## Claude Status

Claude Code is installed and authenticated locally:

```powershell
claude --version
# 2.1.145 (Claude Code)

claude -p --output-format json --model sonnet "Return exactly CLAUDE_CODE_OK and nothing else."
# result: CLAUDE_CODE_OK
```

CLIO does not currently expose a Claude Code provider path. Direct Anthropic
API is available through `CLIO_LM_PROVIDER=anthropic`, but this environment did
not have `ANTHROPIC_API_KEY`, `CLAUDE_API_KEY`, or
`CLAUDE_CODE_OAUTH_TOKEN` set. This is tracked in #246.

## Remaining Work

- Add or document the supported Claude path, then run the same prompt matrix
  against that provider.
- Re-run the full suite on merged `develop` after #243 lands.
