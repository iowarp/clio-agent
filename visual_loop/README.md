# CLIO Visual Loop Handoff

This branch contains the backend side of the TUI presentation work. The matching
frontend branch is `visual_loop` in `D:\Libraries\Documents\projects\gact-tui`.

## What Changed Here

- GACT now emits `expert_handoff` message parts for cross-agent handoffs instead
  of leaving them only inside assistant metadata.
- Nanoagent sessions are materialized with explicit parent/session metadata, agent
  identity, tool count, and readable subagent input text.
- Regression coverage checks that handoff parts and nanoagent session metadata are
  present for the TUI.

## Why This Matters

The TUI needs enough structured information to explain CLIO's hierarchical
behavior: orchestrator routes, expert handoffs, nanoagent work, tool calls, tool
results, and final synthesis. The product issue is not only whether the backend
works; users need to see what happened without decoding raw JSON or guessing from
session names.

## Verification

Run from this repo:

```powershell
uv run ruff check src/clio_agent/gact/app.py tests/test_gact/test_tools_called.py tests/test_gact/test_nanoagents.py
uv run pytest tests/test_gact/test_tools_called.py tests/test_gact/test_nanoagents.py -q
uv run pytest tests/test_gact -q
```

Run the matching TUI tests from the `gact-tui` repo:

```powershell
go test -p 1 ./tui/internal/ui ./tui/internal/client ./emulator/pkg/gact -count=1
go build -p 1 -o tui/gact.exe ./tui
```

Brand/distribution proof for the bundled CLIO TUI lives at:

```text
visual_loop/clio_tui_brand_intro.tape
visual_loop/screenshots/clio_tui_brand_intro.png
visual_loop/screenshots/clio_tui_brand_intro.gif
```

## Recreating Long TUI Sessions

The most useful live transcript source is the ALCF demo benchmark harness:

- `scripts/run_demo_benchmark.py`
- `docs/archive/ALCF_DEMO_BENCHMARK_REPORT.md`

The report documents the final 21-case run, including prompts, provider/model
settings, selected agent, handoffs, tools, child sessions, artifacts, elapsed
time, and caveats. The final local evidence file was:

```text
tmp/clio-demo-benchmark-alcf-metis-20260524-stress-final4.jsonl
```

That JSONL is a local run artifact, so a fresh clone should reproduce sessions
by running the harness against a live GACT backend rather than relying on the
artifact being present.

Important distinction:

- `--output-jsonl` and `--report` save benchmark evidence files for audit and
  report rendering.
- They do not, by themselves, create TUI-visible sessions.
- TUI-visible sessions are created when the harness calls the live backend API:
  `POST /v1/sessions`, `POST /v1/sessions/{id}/messages`, and follow-up polling.
- To inspect those sessions in the TUI, keep the same backend process alive and
  run `gact connect <agent-name>` after the benchmark.
- If the backend is stopped, whether transcripts survive depends on the backend's
  session/message persistence configuration. The JSONL/report are not a drop-in
  replacement for live GACT sessions.

### Backend Setup

From this repo:

```powershell
uv sync --extra dev --extra optimizers
$env:CLIO_AGENT_SRC = "D:\Libraries\Documents\projects\clio-agent"
$env:CLIO_AGENT_MAX_STEPS = "12"
$env:CLIO_GACT_TURN_TIMEOUT_S = "900"
$env:CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS = "5,15"
gact agent deploy clio visual-benchmark
gact agent list
```

Use the `HOST:PORT` from `gact agent list` as the benchmark `--base-url`.
Configure the provider through the TUI or API before running real-provider
cases. The final stress gate used ALCF Metis `gpt-oss-120b`, but for visual-loop
debugging the key requirement is to create rich transcripts, not to depend on a
specific provider.

### Full Stress Run

```powershell
uv run python scripts/run_demo_benchmark.py `
  --base-url http://127.0.0.1:<PORT> `
  --output-jsonl tmp/visual-loop-benchmark.jsonl `
  --report tmp/visual-loop-benchmark-report.md `
  --case-delay-s 2 `
  --require-stress-criteria
```

This creates many sessions and child/nanoagent sessions. It is intentionally
useful for testing sidebar scale, collapsed children, handoff rows, tool-result
rendering, details, scrolling, and memory/context presentation.

After it finishes, verify both layers:

```powershell
# Evidence artifacts exist.
Test-Path tmp/visual-loop-benchmark.jsonl
Test-Path tmp/visual-loop-benchmark-report.md

# Live backend sessions and generated child sessions exist.
Invoke-RestMethod http://127.0.0.1:<PORT>/v1/sessions |
  ConvertTo-Json -Depth 8
```

### Focused Runs For TUI Debugging

Use selected cases to quickly recreate specific visual shapes:

```powershell
# Long NDP/seismic workflow with tier-3 handoffs and many tool results.
uv run python scripts/run_demo_benchmark.py --base-url http://127.0.0.1:<PORT> `
  --case ndp_seismic_waveform_to_plot `
  --output-jsonl tmp/visual-loop-ndp.jsonl `
  --report tmp/visual-loop-ndp.md

# Cross-file nanoagent fan-out with multiple materialized child sessions.
uv run python scripts/run_demo_benchmark.py --base-url http://127.0.0.1:<PORT> `
  --case cross_file_triage_nanoagents `
  --case cross_file_dirty_quality_gate_nanoagents `
  --case reasoning_cross_file_triage_nanoagents `
  --output-jsonl tmp/visual-loop-nanoagents.jsonl `
  --report tmp/visual-loop-nanoagents.md

# Provider/model swap and retained-memory follow-up.
uv run python scripts/run_demo_benchmark.py --base-url http://127.0.0.1:<PORT> `
  --case provider_swap_memory_followup `
  --output-jsonl tmp/visual-loop-provider-swap.jsonl `
  --report tmp/visual-loop-provider-swap.md

# Context pressure and compaction behavior.
uv run python scripts/run_demo_benchmark.py --base-url http://127.0.0.1:<PORT> `
  --case context_pressure_compaction_followup `
  --output-jsonl tmp/visual-loop-compaction.jsonl `
  --report tmp/visual-loop-compaction.md
```

After each run, connect the TUI to the same backend:

```powershell
gact connect visual-benchmark
```

Inspect the generated sessions directly. These cases are meant to expose visual
failures that unit tests miss: hundreds of sessions, child-session grouping, long
raw tool outputs, nested handoffs, details, scrolling to bottom, provider swap
markers, surfaced errors, and artifacts.

## Still Open

- Conversation scroll state is broken after scrolling away from bottom: the
  selection moves but the visible transcript may not return to the bottom.
- Tool/result rendering is still too raw for long scientific workflows.
- Agent, skill, tool, MCP, memory, and context information needs richer drill-down
  views in the TUI.
- Child/nanoagent sessions are now clearer, but the product should decide whether
  they are read-only evidence sessions by default.
