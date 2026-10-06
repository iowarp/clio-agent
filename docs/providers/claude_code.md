# Claude Code provider

Use your Claude Code subscription as a CLIO LM provider without setting an
`ANTHROPIC_API_KEY`.

CLIO runs Claude Code as a DSPy 3.4 engine
(`src/clio_agent/providers/claude_code_engine.py`). When
`CLIO_LM_PROVIDER=claude_code`, `create_lm` builds
`dspy.LM("claude_code/<model>", engine=ClaudeCodeEngine(...),
async_engine=AsyncClaudeCodeEngine(...))` -- no LiteLLM -- and the engine drives the
Claude Agent SDK (`claude_agent_sdk`) through ONE pooled client per GACT session
(`claude_code_sessions.py`).

Claude Code is used only as a model transport: its built-in agent tools, MCP
servers, plugins and skills are disabled, and CLIO's agent loop (`ClioReAct`) and
MCP/tool gateway remain the only tool execution path.

**Requests and tools.** The engine takes a typed `dspy.lm15.Request` and returns a
typed `Response`. The system prompt plus the tool rules and tool list ride
`ClaudeAgentOptions.system_prompt`; the messages render as text
(`lm/engines/text_tools.py`), and the model ends a message with one fenced
`tool_calls` block, which comes back as typed tool calls (an unreadable block is one
`clio_invalid_tool_calls` call whose observation is the parse error). Images and
PDFs ride as native content blocks (`claude_code_multimodal.native_blocks`: supported
media types only, size-bounded, remote image URLs only for allowlisted hosts).

**Stateful sessions.** Inside an agent loop the engine keeps one Claude Code session
per conversation (GACT session + agent scope, model, cwd, thinking) and sends only
the messages after what the session already holds, under the same session id. The
agent's context spans turns and is append-only, so the session carries over from one
turn to the next as well. Any
other call opens a new session and sends in full, with a typed reset reason on the
`provider.stateful` audit row (`first_call` / `prefix_mismatch` / `ops_reset` /
`session_evicted` / `provider_error`). A pooled client that is reconnected, reaped,
released or dies announces the drop synchronously, and that session's conversations
reset (`session_evicted`) -- a delta never reaches a fresh subprocess. Capacity:
`providers.claude_code.stateful_capacity` / `CLIO_CLAUDE_CODE_STATEFUL_CAPACITY`.

**Streaming.** Thinking streams as thinking, text streams live up to the tool-call
block, then the calls and the usage (cache reads and writes included).

**Errors.** A timeout or a dead transport raises a `dspy.lm15` type (DSPy retries it);
a refused sign-in, a rejected model and an exhausted plan raise clio's typed errors,
which the agent loop re-raises as themselves.

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
