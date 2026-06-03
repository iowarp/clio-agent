# Case 01: Genomics Cohort QC And Variant Triage

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to review a cohort of genome samples, detect quality problems, identify
which samples should be dropped, and explain why. The prompt must not name the
internal genomics experts, tools, or intended fan-out path.

## Expected Agent Blueprint

Primary pack: `genomics-review`, or a later researched replacement pack.

The expected hierarchy should include a cohort/root expert, per-sample metrics,
cohort outlier detection, manifest reconciliation, and variant/reference
review where needed.

## Semantics To Prove

- Multi-branch genomics decomposition.
- Per-sample or per-record fan-out with merge evidence. If this uses spawned
  workers, the spawn/merge semantics must be declared by the pack rather than
  relying on hardcoded Python expert behavior; this depends on
  `iowarp/clio-agent#629`.
- Manifest or metadata reconciliation separate from raw variant inspection.
- Tool-grounded FASTA/VCF or cohort-QC evidence.
- Parent synthesis into a drop/keep advisory.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
