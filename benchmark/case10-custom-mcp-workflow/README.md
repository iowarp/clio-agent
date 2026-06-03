# Case 10: Custom MCP Packaged-Tool Workflow

Status: not yet passed as a live full-agent case.

Issue checklist entry: `iowarp/clio-agent#628`.

Historical focused evidence:

- `../MARKETPLACE_MCP_SCOPE_REPORT.md`
- `../MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md`

## Benchmark Prompt Intent

Ask CLIO to complete a workflow that requires a pack-local MCP tool bundle,
without naming MCP internals. The case should prove descriptor, trust, launch,
scope, tool call, and parent synthesis behavior.

## Expected Agent Blueprint

Primary pack: `mcp-calculator-smoke` is acceptable only as infrastructure
proof. The demo benchmark should prefer a researched scientific pack with a
custom MCP or a stronger replacement.

## Semantics To Prove

- Pack-local MCP descriptor is disabled until explicitly trusted/enabled.
- Launch/probe/call happens through the CLIO MCP runtime.
- Only the owning expert sees the MCP tools.
- The live trace records MCP lifecycle and tool-call evidence.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
