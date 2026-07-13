# Case 09: Scientific Format Bridge Integrity

Status: not passed.

## Prompt Intent

Ask CLIO to validate or bridge scientific data across formats while preserving
schema, dtype, shape, units, provenance, and known lossy-conversion policy.

## Semantics To Prove

- Source inspection, conversion policy, lossy policy, integrity validation, and
  visual/statistical checks are separate expert work.
- Unsupported or lossy conversions are surfaced honestly.
- Parent answer distinguishes verified equivalence from unverified similarity.

## Required Expert Decomposition

- `main`: owns the integrity question and decides which checks are required.
- `source_inspect`: records schema, dtype, shape, units, metadata, and
  provenance from the source.
- `target_inspect`: records equivalent evidence from the target or converted
  file.
- `conversion_policy`: defines acceptable transformations and known lossy
  behavior.
- `integrity`: compares source/target data and metadata.
- `visual_or_stat_check`: samples data or plots distributions where useful.

The case is not a pass if it merely confirms that two files can be opened.

## Current Core Problem

Prior format cases were too focused on whether tools run. The benchmark needs
data integrity semantics under realistic ambiguity.
