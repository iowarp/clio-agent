# Genomics Cohort QC Infrastructure

Date: 2026-06-03

Source benchmark case:
`CLIO_HIERARCHICAL_AGENT_BENCHMARK_CASES_SOURCE.md`, Case 1 —
Genomics cohort QC.

## What Changed

CLIO now has a reusable VCF cohort QC tool for small or region-subset VCFs:
`genomics_vcf_cohort_qc`.

The tool computes per-sample genotype metrics that the benchmark case needs:

- call rate and missing calls,
- heterozygosity,
- het/hom ratio,
- genotype count buckets,
- cohort mean/stdev summaries,
- low-call-rate and high-heterozygosity flags.

This closes the first backend gap between the current shallow
`genomics_summarize_vcf` handoff review and the benchmark source requirement
for cohort-level QC evidence.

## Marketplace Wiring

The `genomics-review` marketplace pack now includes a durable tier-2
`cohort_qc` expert that declares `genomics_vcf_cohort_qc`. The root expert can
delegate multi-sample VCF quality prompts to that expert without hardcoding a
benchmark prompt path.

## Evidence

Local backend checks:

```bash
uv run ruff check src/clio_agent/tools/servers/genomics_server.py src/clio_agent/tools/catalog.py tests/test_tools/test_genomics_server.py
uv run mypy src/
uv run pytest tests/test_tools/test_genomics_server.py tests/test_tools/test_gateway.py tests/test_gact/test_agent_blueprints.py::test_agent_blueprint_validation_reports_unknown_tools tests/test_scripts/test_validate_marketplace_blueprints.py -q
```

Marketplace preflight:

```bash
uv run python scripts/validate_marketplace_blueprints.py /home/jcernuda/clio-agent-marketplace --require-complex-count 3 --require-self-contained-mcp-count 1 --require-hook-descriptor-count 1
```

Result: pass. `genomics-review` validates with six experts, five hierarchy
edges, three levels, and three declared tools.

## What This Does Not Claim Yet

This is infrastructure readiness, not the final public-demo benchmark result.
The full Case 1 benchmark still needs a generated or fetched multi-sample
cohort fixture with planted defects, a real session run through the
`genomics-review` pack, and evidence that the hierarchy catches the planted
low-call-rate / excess-heterozygosity / manifest-reconciliation defects.
