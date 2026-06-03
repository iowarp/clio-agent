# Case 05: HPC I/O Regression Root-Cause Analysis

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to compare baseline and candidate HPC I/O traces, identify what
regressed, and recommend what to rerun or inspect next.

## Expected Agent Blueprint

Primary pack: `hpc-io-regression`, or a later researched replacement pack.

## Semantics To Prove

- Baseline and candidate trace ingestion as separate child work.
- Regression diff and root-cause analysis as separate expert stages.
- Tool-grounded Darshan or trace evidence.
- Parent synthesis with rerun advice tied to observed metrics.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
