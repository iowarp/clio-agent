# First-Wave Marketplace Runner Coverage

Issue: iowarp/clio-agent#604

Benchmark source:
`CLIO_HIERARCHICAL_AGENT_BENCHMARK_REVIEW.md`, first-wave priorities.

## What Changed

The demo benchmark runner now has canonical marketplace cases and deterministic
local fixtures for the first-wave scientific packs:

- genomics cohort QC through `genomics-review` and
  `genomics_vcf_cohort_qc`;
- proteomics LFQ differential abundance through `proteomics-mzml-review` and
  `mass_spec_lfq_differential_abundance`;
- HPC I/O regression through `hpc-io-regression` and Darshan comparison tools;
- scientific format bridge integrity through `format-bridge` and
  `format_convert_hdf5_to_parquet`;
- terrain point-cloud suitability through `terrain-suitability`,
  `terrain_pointcloud_read`, and `terrain_dem_terrain`.

The `marketplace_agents` lane now includes those cases alongside the existing
genomics reference/variant, materials, geospatial, proteomics mzML, and seismic
waveform cases.

## Evidence Added

Focused tests cover:

- deterministic generation of the new LFQ, HPC, format-bridge, and terrain
  fixtures;
- marketplace lane selection for the expanded case set;
- canonical complex-hierarchy requirements for HPC, format bridge, and terrain
  cases;
- full benchmark runner test coverage for report rendering and lane audits.

## Not Claimed Yet

This is benchmark-runner coverage, not final real-provider benchmark evidence.
The watched manual/demo run still needs to execute the expanded
`marketplace_agents` lane with the current marketplace source, inspect live
semantic events and artifacts, and record the final pass/fail evidence.
