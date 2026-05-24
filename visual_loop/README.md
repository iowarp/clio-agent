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

## Still Open

- Conversation scroll state is broken after scrolling away from bottom: the
  selection moves but the visible transcript may not return to the bottom.
- Tool/result rendering is still too raw for long scientific workflows.
- Agent, skill, tool, MCP, memory, and context information needs richer drill-down
  views in the TUI.
- Child/nanoagent sessions are now clearer, but the product should decide whether
  they are read-only evidence sessions by default.
