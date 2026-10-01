# Claude Code provider

Use your Claude Code subscription as a CLIO LM provider without setting an
`ANTHROPIC_API_KEY`.

CLIO registers a LiteLLM `CustomLLM`
(`src/clio_agent/providers/claude_code_litellm.py`) for the
`claude_code/` model prefix. When `CLIO_LM_PROVIDER=claude_code`, DSPy
constructs `dspy.LM(model="claude_code/<model>")`, LiteLLM routes that
call to the custom handler, and the handler drives the Claude Agent SDK
(`claude_agent_sdk`) through ONE pooled client per GACT session
(`claude_code_sessions.py`). The SDK is the only transport (the old
one-`claude -p`-per-call `exec` transport is deleted).

Claude Code is used only as a model transport: its built-in agent tools are
disabled, and CLIO's agent loop (`ClioReAct`) and MCP/tool gateway remain the
only tool execution path.

**Stateful sessions.** Inside one agent-loop forward, the transport keeps one
Claude Code session and sends only the newly appended messages on each
following call (`claude_code_stateful.py`, sharing the delta detector in
`stateful_common.py` with the Codex SDK transport); any other call is a full
send under a fresh session, with a typed reset reason on the
`provider.stateful` audit row. The session is released when the forward ends.

**Streaming.** Text and thinking stream live (`astreaming`) through the LM
token hooks, so answer deltas arrive with `stream_source="live"`.

## Setup

Install and authenticate Claude Code:

```powershell
claude --version
claude login
```

Run CLIO/GACT with Claude Code:

```powershell
$env:CLIO_LM_PROVIDER='claude_code'
$env:CLIO_LM_MODEL='sonnet'
uv run clio-agent serve --host 127.0.0.1 --port 17920
```

`sonnet` is the recommended default alias because Claude Code resolves it
to the currently available Sonnet model for the authenticated account.
Full model names such as `claude-sonnet-4-6` can also be used when the
local Claude Code version supports them.

## Troubleshooting

**`claude` not on PATH.** Install Claude Code and verify `claude --version`
works from the same shell that starts CLIO.

**Authentication errors.** Run `claude login`. This provider uses Claude
Code subscription auth, not `ANTHROPIC_API_KEY`.

**Wrong provider for direct API usage.** Use `CLIO_LM_PROVIDER=anthropic`
when you want direct Anthropic API billing with `ANTHROPIC_API_KEY`.

**Unexpected tool behavior.** The provider disables Claude Code tools.
If a CLIO turn uses a tool, it should appear in CLIO/GACT tool telemetry,
not in Claude Code's internal tool system.

**No live streaming.** This provider shells out to `claude -p`, which returns
a completed JSON result. Use GACT stream metadata to distinguish this
post-hoc delivery from providers that emit live token chunks.

## Benchmark Lane

Run the CLIO real-provider benchmark lane against a live GACT backend that was
started with `CLIO_LM_PROVIDER=claude_code`:

```powershell
uv run python scripts/run_demo_benchmark.py `
  --base-url http://127.0.0.1:17920 `
  --lane claude_code `
  --output-jsonl tmp/clio-demo-benchmark-claude-code.jsonl `
  --report tmp/clio-demo-benchmark-claude-code.md `
  --require-lane-criteria
```

The Claude lane records provider/model evidence, planner/routing behavior,
tool-call argument generation, stream provenance, cancellation surfacing, and
structured error surfacing separately from the ALCF/Qwopus benchmark report.
The HDF5 tool/error cases pin the turn to CLIO's built-in `data` agent so
ambient user skills cannot change the benchmark target; the separate no-guard
cross-file case remains the planner/routing reliability proof.
