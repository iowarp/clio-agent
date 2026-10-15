---
name: hdf5
description: Use for HDF5 files (.h5, .hdf5, netCDF-4, NeXus, NWB, .h5ad and others) — what a file contains and where its main data is, reading and writing with h5py, chunking, compression and performance, cloud and S3 access, parallel I/O, file drivers, and ingest into clio-core.
---

# HDF5

This skill routes HDF5 questions to the reference guide that answers them.
Load the guides you need with `load_skill`, for example
`load_skill("hdf5", file="references/chunking.md")`, or several at once with
`files=[...]`.

## How to work

- **Inspect before advising.** When a file is available, look at it first;
  never invent file facts. Report the paths, shapes, dtypes, chunking and
  attributes you actually saw.
- **Use the HDF5 tools if you have them** (`open_file`, `list_keys`,
  `get_by_path`, `list_attributes`, `read_partial_dataset`, `close_file`, …).
  Otherwise use h5py through code execution: open read-only with
  `h5py.File(path, "r")`, list with `list(f)` and `f.attrs`, and sample
  values with slices such as `dset[:10]`. Never read a whole large dataset
  to look at it.
- **Start with the convention.** For an unfamiliar file, identify the
  community convention it follows before walking it:
  `references/conventions.md`. It tells you where the primary data is.
- **Check versions before version-specific advice.** The guides describe
  HDF5 2.2 and h5py 3.16. Many behaviors and defaults changed in HDF5 2.0;
  in h5py, check `h5py.version.hdf5_version` and `h5py.version.version`.
- **Advice is not measurement.** Present tuning advice as recommendations
  unless you measured it.

## Which guide

**What is in a file**

| Guide | Use for |
|---|---|
| `references/conventions.md` | Which community convention a file follows and where its main data is |
| `references/datatypes.md` | Compound, string, enum, variable-length and other datatypes; h5py type mapping |
| `references/dimension_scales.md` | Dimension scales and coordinate variables |
| `references/cf_compliance.md` | Whether a netCDF-4/HDF5 file meets the CF conventions |
| `references/region_references.md` | Object and region references |
| `references/map_objects.md` | HDF5 map objects (key-value stores) |

**Layout and performance**

| Guide | Use for |
|---|---|
| `references/chunking.md` | Storage layout, chunk shape and the chunk cache |
| `references/filters.md` | Compression and other filters, and choosing among them |
| `references/io.md` | General I/O tuning: buffers, caches, access patterns |
| `references/file_space.md` | File size, free space, page allocation, repacking |

**Remote and cloud.** Choose by how the data is reached:
reading an existing file directly from S3 or HTTP is ROS3; going through a
service layer is HSDS; changing how a file is *written* so remote reads are
fast is the cloud-optimized guide.

| Guide | Use for |
|---|---|
| `references/ros3_vfd.md` | Reading HDF5 files directly from S3 or S3-compatible storage |
| `references/hsds.md` | HDF5 through the HSDS REST service (S3, Azure, POSIX backends) |
| `references/cloud_optimized.md` | Writing or rewriting files for efficient remote access |

**Concurrency and parallel I/O.** One writer with live readers is SWMR;
many MPI processes writing one file is parallel HDF5; at very large scale,
subfiling splits that file across subfiles.

| Guide | Use for |
|---|---|
| `references/swmr.md` | Single-writer/multiple-reader access to a file being written |
| `references/parallel.md` | MPI parallel HDF5 and collective I/O |
| `references/subfiling_vfd.md` | The subfiling driver for large-scale parallel writes |

**Drivers, connectors and virtual data**

| Guide | Use for |
|---|---|
| `references/core_vfd.md` | In-memory files with the core driver |
| `references/onion_vfd.md` | Revision history with the onion driver |
| `references/vds.md` | Virtual datasets that combine data from several files |
| `references/vol_usage.md` | VOL connectors such as async, cache, DAOS and REST |

**CLIO**

| Guide | Use for |
|---|---|
| `references/clio_core_ingest.md` | Whether to ingest an HDF5 file into clio-core or read it with h5py |
| `references/omni_selective.md` | OMNI configurations that load only the datasets a task needs |

Questions often span guides: a slow remote read may need `ros3_vfd.md` and
`chunking.md`; compression choices depend on `chunking.md` and
`filters.md`. Load each guide whose topic the question touches.
