# Case 08: HPC I/O Regression Root-Cause

Status: not passed.

## Prompt Intent

Ask CLIO to compare baseline and candidate HPC I/O traces, identify a
regression, and propose reruns or instrumentation.

## Semantics To Prove

- Baseline and candidate ingestion are separate child work.
- Regression diff and root-cause analysis are separate stages.
- Evidence is grounded in Darshan or equivalent trace metrics.
- Parent synthesis ties recommendations to observed counters, not generic HPC
  advice.

## Required Expert Decomposition

- `main`: owns regression question and assigns baseline/candidate work.
- `baseline_ingest`: extracts metrics from the baseline trace.
- `candidate_ingest`: extracts metrics from the candidate trace.
- `regression_diff`: compares counters, timings, access patterns, and metadata.
- `root_cause`: reasons about likely causes and missing instrumentation.
- `rerun_plan`: recommends concrete reruns, counters, or tracing changes.

The case fails if the answer is generic HPC advice or if baseline and candidate
metrics are not independently grounded.

## Current Core Problem

Existing infrastructure docs do not constitute a live, realistic regression
investigation.
