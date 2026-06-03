# Case 02: Genomics Cross-Session Memory Follow-Up

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to use what a previous genomics session found and perform a follow-up
analysis or recommendation in a new session. The prompt should be natural, for
example "based on what we found yesterday" style language, without naming the
memory tools.

## Expected Agent Blueprint

Primary pack: `genomics-review`, or a later researched replacement pack.

## Semantics To Prove

- Workspace-scoped memory access with explicit same-workspace intent.
- No access to unrelated workspace/global sessions.
- Memory search/read evidence surfaced as semantic events.
- Genomics expert continues from retrieved compact evidence instead of
  inventing prior results.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
