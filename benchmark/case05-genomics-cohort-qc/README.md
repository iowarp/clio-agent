# Case 05: Genomics Cohort QC

Status: not passed.

## Prompt Intent

Ask CLIO to review a cohort of genome samples, detect quality problems, decide
which samples should be dropped or flagged, and explain why.

## Semantics To Prove

- Per-sample or per-file fanout with merge evidence.
- Manifest reconciliation separate from raw FASTA/VCF inspection.
- Reference quality and variant interpretation as separate expert work.
- Parent synthesis into drop/keep/verify recommendations.

## Required Expert Decomposition

- `main`: owns cohort-level question and merges all child results.
- `manifest`: validates sample IDs, file presence, metadata consistency, and
  expected cohorts/conditions.
- `per_sample_qc`: fans out over samples or files and computes quality metrics.
- `cohort_outliers`: compares samples against cohort distributions and flags
  outliers.
- `reference_quality`: inspects reference composition and suitability.
- `variant_review`: interprets variant effects where relevant.
- `handoff`: creates collaborator-facing keep/drop/verify recommendations.

One FASTA plus one VCF summary is not complex enough. The case needs enough
samples or records to require fanout and merge behavior.

## Current Core Problem

Existing genomics evidence is too narrow. The final case must be cohort-shaped,
not a single FASTA/VCF summary.
