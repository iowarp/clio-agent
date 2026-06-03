# Case 11: Hooks, Semantic Logging, And Streaming Observability

Status: not yet passed as a live full-agent case.

Issue checklist entry: `iowarp/clio-agent#628`.

Historical focused evidence:

- `../MARKETPLACE_PACKAGED_HOOK_REPORT.md`

## Benchmark Prompt Intent

Ask CLIO to run a normal workflow while hooks, semantic logging, and streaming
events are enabled. The human monitor should see the operation evolve, not only
receive a final answer.

## Expected Agent Blueprint

Any researched scientific pack that also includes or enables a packaged hook.

## Semantics To Prove

- Streamed session events arrive during execution.
- Durable semantic logs include LLM calls, tool/MCP calls, delegation, memory
  or artifact access, hooks, errors, and recovery where relevant.
- Packaged hook invocation has trust/provenance evidence.
- Final result matches the live stream and semantic trace.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
