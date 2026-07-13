# Case 07: Proteomics LFQ Cohort Review

Status: not passed.

## Prompt Intent

Ask CLIO to review proteomics quality, search readiness, and differential LFQ
signals for a cohort-level collaborator handoff.

## Semantics To Prove

- mzML or LFQ evidence is inspected with proteomics-specific tools.
- Spectra quality, search readiness, and differential analysis are distinct
  expert stages.
- Parent synthesis distinguishes proven signal from preprocessing or metadata
  caveats.

## Required Expert Decomposition

- `main`: owns the collaborator handoff question and merges proteomics branches.
- `raw_or_mzml_qc`: inspects spectra and acquisition quality.
- `search_readiness`: checks metadata, enzyme, modifications, database, and
  identification readiness.
- `lfq_quant`: profiles LFQ matrix quality, missingness, replicate structure,
  and normalization caveats.
- `differential`: evaluates candidate condition contrasts when data supports
  it.
- `handoff`: produces a decision-oriented readiness summary.

A single mzML parser invocation is not enough. The case must force several
proteomics-specific decisions and at least one unsupported/missing-data caveat.

## Current Core Problem

The existing case is too small and tool-centered. It must become a realistic
cohort review with several failure modes and decision points.
