# Case 04: Proteomics Raw/mzML Conversion And Validation

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to decide whether a proteomics raw/exported format is usable for
downstream analysis and whether conversion preserved the evidence needed for
search or quantification.

## Expected Agent Blueprint

Primary pack: `proteomics-mzml-review`, or a later researched replacement pack.

## Semantics To Prove

- Format-readiness expert separate from spectra or differential review.
- Evidence of raw/mzML inspection or conversion validation.
- Surfaced unsupported-format behavior when the input cannot be analyzed.
- Parent synthesis that does not overclaim conversion success.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
