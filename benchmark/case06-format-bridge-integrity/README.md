# Case 06: Scientific Format Bridge Integrity Workflow

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

Related reopened issue: `iowarp/clio-agent#617`.

## Benchmark Prompt Intent

Ask CLIO to bridge or validate scientific data across formats while preserving
schema, dtype, shape, units, provenance, and known lossy-conversion policy.

## Expected Agent Blueprint

Primary pack: `format-bridge`, or a later researched replacement pack.

## Semantics To Prove

- Source inspection, conversion policy, lossy-policy, integrity, and visual
  check as separate expert work.
- Tool-grounded HDF5/Parquet or equivalent format evidence.
- Empty-final-answer recovery from tool-backed experts, as tracked by #617.
- Parent synthesis that distinguishes proven integrity from unsupported or
  lossy conversion.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
