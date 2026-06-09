---
name: hdf5-dimension-scales
description: "dimension scales", "coordinate arrays", "axis labels", "named dimensions", "NetCDF compatibility", "CF conventions", "H5DS", "attach coordinates to dataset axes", "dimension labels in HDF5", "xarray HDF5 coordinates"
version: 0.1.0
---

# HDF5 Dimension Scales

## Purpose

Attach named coordinate arrays to dataset axes, making datasets self-describing and enabling compatibility with NetCDF/CF conventions. Part of the HDF5 High-Level (HL) `H5DS*` API.

**Related skills**: `hdf5-scientific-publishing` for metadata standards, `hdf5-vds` for multi-source layouts.

## What Problem Dimension Scales Solve

HDF5 datasets carry no inherent axis semantics — a 2D array has no built-in notion of which axis is latitude vs. longitude or what the coordinate values are. Dimension scales make axes self-documenting and interoperable with CF/NetCDF-aware tools (xarray, Panoply, ncview).

## Architecture

Dimension scales are implemented entirely via HDF5 attributes on ordinary datasets — no separate storage mechanism:

- **Scale dataset**: a normal dataset marked with `CLASS="DIMENSION_SCALE"` and `NAME="..."` attributes
- **Attached dataset**: gains a `DIMENSION_LIST` attribute — a variable-length object-reference array, one entry per axis
- **Back-reference**: `REFERENCE_LIST` on the scale dataset points to all datasets it's attached to

One scale can be shared across many datasets (e.g., a common time axis stored once). No data is duplicated.

## API Options

h5py does not directly expose the C `H5DS*` API. Options in order of preference:

| Approach | Best For |
|---|---|
| `h5netcdf` / `netCDF4` library | NetCDF/CF-compatible output (handles scales automatically) |
| h5py `dims` interface | h5py-native workflows |
| C `H5DS*` API | Full control, C/C++ codebases |

**h5py `dims` interface**:
- `dset.dims[0].label = "time"` — set axis label
- `dset.dims[0].attach_scale(scale_dset)` — attach a scale dataset
- `dset.dims[0].detach_scale(scale_dset)` — remove attachment
- `dset.dims[0][0]` — retrieve first attached scale

**Marking a dataset as a scale** (h5py ≥ 3.x):
- Pass `make_scale=True` to `create_dataset`, or
- Call `h5py.h5ds.set_scale(scale_dset.id, b"name")` with bytes-encoded name

## Do / Do Not

**Do**:
- Share one scale across multiple datasets with the same axis (single storage, many references)
- Store coordinate metadata (`units`, `calendar`, `axis`) as attributes on the scale dataset
- Use `netCDF4` or `h5netcdf` when strict CF-conventions compliance is the goal
- Set both `label` (on the attachment) and `NAME` (on the scale) — tools read either or both
- Attach scales before closing the file — references are validated at close

**Do not**:
- Confuse axis `label` with scale `NAME` — they are independent and serve different tools
- Delete a scale dataset while it is still referenced — leaves dangling `DIMENSION_LIST` entries with no automatic cleanup
- Assume all HDF5-reading tools understand dimension scales — raw h5py returns object references, not coordinate values
- Use dimension scales as a substitute for compression or chunking metadata

## Tool Interoperability

**Understands dimension scales**: xarray (`engine='h5netcdf'`), Panoply, ncview, ncbrowse

**Does not automatically interpret**: raw h5py reads (returns object refs), h5dump without `-d` flag, tools treating HDF5 as generic key-value store

**xarray note**: dimension name in the resulting Dataset comes from the scale's `NAME` attribute, not from `dset.dims[0].label`. Mismatch causes confusing coordinate labeling.

## Common Pitfalls

**Dangling references**: Deleting a scale dataset does not remove `DIMENSION_LIST` entries on attached datasets. Always detach before deleting.

**Bytes vs. str for NAME**: `H5DS_set_scale` takes a C string. In h5py, pass `b"name"` (bytes), not a Python str.

**Multiple scales per dimension**: One dimension can have multiple attached scales (e.g., time index + timestamp string). `dset.dims[0]` returns the first; iterate to access all.

**h5repack and scale references**: Repacking a file that contains dimension scale references may invalidate `REFERENCE_LIST` entries if dataset tokens change. Re-attach after repacking if needed.

## When to Use

- Scientific datasets where axis coordinates are meaningful (lat/lon, time, wavelength, pressure)
- Files targeting xarray, Panoply, or any CF-aware tool
- Archival data requiring self-description without external documentation
- Any workflow requiring NetCDF interoperability without switching file formats

## When NOT to Use

- Pure performance-oriented files with no interoperability requirement
- Axes with no physical coordinates (ML feature indices, row IDs)
- Short-lived scratch files where attribute overhead isn't worthwhile

## Citations and References

- [HDF Group: Dimension Scales Specification](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_d_s.html)
- [HDF Group: Using Dimension Scales](https://support.hdfgroup.org/documentation/hdf5-docs/advanced_topics/DimensionScales.html)
- [h5py: Dimension Scales](https://docs.h5py.org/en/stable/high/dims.html)
- [CF Conventions: Coordinate Variables](http://cfconventions.org/cf-conventions/cf-conventions.html#coordinate-types)
