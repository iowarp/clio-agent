# Case 12: Multi-Agent Marketplace Swap With Workspace Isolation

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

Historical focused evidence:

- `../MARKETPLACE_WORKSPACE_MEMORY_SCOPE_REPORT.md`

## Benchmark Prompt Intent

Run two different marketplace Agent Blueprints in different workspaces or
sessions, then ask a follow-up that should only see the intended workspace
memory and active pack.

## Expected Agent Blueprint

At least two researched marketplace packs, for example genomics plus HPC or
seismic plus terrain.

## Semantics To Prove

- Workspace-local pack/session activation.
- No accidental access to unrelated workspace sessions.
- Same-workspace cross-session memory access only with explicit intent.
- Per-session pack swap changes available experts/tools/prompts.
- Evidence shows which pack and workspace were active for each turn.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
