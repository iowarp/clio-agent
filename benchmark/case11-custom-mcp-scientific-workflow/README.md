# Case 11: Custom MCP Scientific Workflow

Status: not passed.

## Prompt Intent

Ask CLIO to complete a scientific workflow that needs a pack-local MCP server or
tool bundle, without mentioning MCP internals.

## Semantics To Prove

- Pack-local MCP descriptor trust, launch, probe, and call are visible.
- Only the owning expert sees the MCP tools.
- MCP output feeds parent analysis rather than being the whole answer.
- Trace records lifecycle, tool-call, and provenance evidence.

## Required Expert Decomposition

- `main`: owns the scientific task and identifies the pack-local capability.
- `mcp_runtime`: handles trust, launch, probe, and call evidence.
- `domain_analysis`: uses MCP results as one input to a scientific judgment.
- `audit`: verifies tool scope and provenance.

This cannot be a calculator demo. The MCP server must contribute to a real
scientific workflow that would not work with generic built-in tools alone.

## Current Core Problem

Calculator or hook examples are infrastructure proofs, not public scientific
benchmark cases.
