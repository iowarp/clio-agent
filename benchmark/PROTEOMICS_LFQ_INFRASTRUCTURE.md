# Proteomics LFQ Infrastructure

Date: 2026-06-03

Source benchmark case:
`CLIO_HIERARCHICAL_AGENT_BENCHMARK_CASES_SOURCE.md`, Case 5 —
Proteomics LFQ differential abundance.

## What Changed

CLIO now has a reusable LFQ differential-abundance tool:
`mass_spec_lfq_differential_abundance`.

The tool is designed for compact MaxQuant-style `proteinGroups.txt` tables or
simple LFQ TSV/CSV matrices. It provides the matrix-level evidence that the
existing mzML handoff review could not:

- condition-column discovery from user-provided column fragments,
- contaminant / reverse-row filtering,
- raw versus median-normalized log2 intensity comparison,
- per-protein group means and log2 fold changes,
- simple two-group differential scores and adjusted rankings,
- sample missingness,
- optional spike-in quality scoring against an expected log2 fold change.

This moves the Proteomics LFQ benchmark case from "missing backend tool" to
"ready for a marketplace expert and real session benchmark fixture."

## Evidence

Local backend checks:

```bash
uv run ruff check src/clio_agent/tools/servers/mass_spec_server.py src/clio_agent/tools/catalog.py tests/test_tools/test_mass_spec_server.py
uv run mypy src/
uv run pytest tests/test_tools/test_mass_spec_server.py tests/test_tools/test_gateway.py::test_gateway_has_namespaced_tools -q
```

The focused tests use a synthetic LFQ table with an inflated raw spike-in fold
change and a sample-level loading shift. The tool selects median normalization,
filters the contaminant row, reports sample missingness, and recovers the
positive spike-in direction.

## What This Does Not Claim Yet

This is infrastructure readiness, not the final public-demo benchmark result.
The full Case 5 benchmark still needs a marketplace LFQ expert, a real or
generated spike-in fixture with known ground truth, and a real session run that
shows the hierarchy selecting a normalization path before differential
abundance reporting.
