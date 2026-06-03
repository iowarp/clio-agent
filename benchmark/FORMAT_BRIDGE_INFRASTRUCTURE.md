# Scientific Format Bridge Infrastructure

Issue: iowarp/clio-agent#600

Benchmark source: Case 9, scientific format bridge with integrity guard.

## What Changed

CLIO now has a reusable format bridge tool:

- `format_convert_hdf5_to_parquet` converts compatible one-dimensional HDF5
  datasets to Parquet.
- It validates source and output paths through CLIO file policy before writing.
- It flags unsafe or lossy dtype cases instead of silently coercing them:
  complex scalars, float16, uint64 values that overflow int64 compatibility,
  and datetime-like logical types.
- It returns integrity evidence for converted columns, including row counts,
  NaN counts, and per-column checksums before and after the Parquet write.

This gives the benchmark a real inspect/convert/integrity substrate for the
case where a single-shot converter would otherwise truncate or overclaim.

## Evidence Added

Focused tests cover:

- conversion of a gzip-compressed float column with a NaN at index 13;
- preservation of string values and NaN counts through Parquet;
- explicit lossy/unsafe flags for float16, complex, uint64 overflow, and
  datetime-like logical columns;
- out-of-root write denial with no output file written;
- gateway exposure through `format_convert_hdf5_to_parquet`.

## Not Claimed Yet

This is not the final real-provider end-to-end benchmark result. The manual
demo-readiness run still needs to instantiate a Format Bridge marketplace pack,
exercise inspect -> convert/policy -> integrity -> visualization delegation,
and inspect semantic logs/artifacts for the expected hierarchy.
