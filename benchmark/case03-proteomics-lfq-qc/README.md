# Case 03: Proteomics LFQ Differential Quality Review

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to review a proteomics LFQ or mzML dataset for quality, differential
signals, and handoff readiness. The prompt must not name mass-spec tools or
expert routes.

## Expected Agent Blueprint

Primary pack: `proteomics-mzml-review`, or a later researched replacement pack.

## Semantics To Prove

- Proteomics-specific routing and expert hierarchy.
- Independent spectra quality, search-readiness, and LFQ/differential checks.
- Tool-grounded mzML/LFQ evidence.
- Parent merge into collaborator-facing quality and interpretation guidance.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
