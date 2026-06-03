# Case 09: NDP Or External Scientific Catalog Recovery Path

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

## Benchmark Prompt Intent

Ask CLIO to find a scientific dataset from an external catalog, handle an
unavailable or unsuitable first candidate, and recover by trying a valid
alternative or surfacing an honest bounded failure.

## Expected Agent Blueprint

Primary pack: a researched NDP, OGC, seismic, terrain, or catalog-recovery pack.

## Semantics To Prove

- Catalog discovery separated from candidate selection and download/staging.
- Failure returns to the owning parent expert with compact evidence.
- Parent attempts a reasonable alternate source or reports a bounded surfaced
  error without inventing downstream artifacts.
- Download/staging provenance includes source, size or bound, and artifact path
  when successful.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
