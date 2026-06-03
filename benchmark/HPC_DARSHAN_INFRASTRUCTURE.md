# HPC Darshan Regression Infrastructure

Issue: iowarp/clio-agent#598

Benchmark source: Case 6, HPC I/O performance regression.

## What Changed

CLIO now has reusable HPC/Darshan text-trace tools for the benchmark's
two-version regression workflow:

- `hpc_parse_darshan_text` parses Darshan-style text reports and compact
  normalized key/value traces into runtime, I/O timing, operation counts, byte
  counts, collective/independent hints, transfer-size evidence, and partial
  trace warnings.
- `hpc_compare_darshan_traces` compares a baseline trace against a candidate
  trace, aligns metrics, ranks deltas, and emits a compact root-cause signal
  such as write-path regression.

The implementation is intentionally text-first and dependency-light. It gives
the benchmark and marketplace a real tool-grounded path for Darshan reports
without requiring the native Darshan runtime on every demo machine.

## Evidence Added

Focused tests cover:

- extracting runtime, write time, bytes, operation counts, and transfer sizes;
- detecting a synthetic injected write regression with about +147 percent
  write time and +18 percent runtime;
- surfacing truncated/partial trace warnings instead of overclaiming;
- gateway exposure through `hpc_compare_darshan_traces`.

## Not Claimed Yet

This is not the final real-provider end-to-end benchmark result. The manual
demo-readiness run still needs to instantiate an HPC marketplace pack, delegate
baseline and candidate ingest, merge at a regression-diff expert, and inspect
the semantic trace/artifacts for the expected hierarchy.
