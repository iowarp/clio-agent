---
name: hdf5-region-references
description: "region reference", "HDF5 reference", "hyperslab reference", "point selection reference", "H5R_REGION", "annotate dataset subset", "reference to dataset region", "ROI in HDF5", "store selections HDF5", "object reference HDF5", "H5Rcreate"
version: 0.1.0
---

# HDF5 Region References

## Purpose

Region references (`H5R_REGION`) store persistent, typed pointers to a specific hyperslab or point-selection within a dataset. They enable annotation of data subsets — ROIs, detected features, calibration regions — without copying data.

**Related skills**: `hdf5-vds` for logical dataset composition across files, `hdf5-swmr` (region references are restricted under SWMR).

## What Region References Solve

Annotating a subset normally requires copying data or storing raw offsets/shapes as plain integers. Region references encode the selection in HDF5's type system — they survive file reorganization better than hardcoded offsets and are readable by any HDF5-aware tool without custom parsing.

**Typical use cases**: ROI annotation in imaging/spectroscopy data, linking derived results to exact source regions, building index datasets where each entry references a time-series segment.

## Architecture

A region reference is a fixed-size opaque blob encoding:
- A token identifying the referenced dataset within the file
- The selection type (hyperslab or point list)
- The selection geometry (start/stride/count/block or point coordinates)

Size: 12 bytes in HDF5 ≤ 1.12; 16 bytes with extended references in HDF5 1.14+ (`H5R_ref_t`). h5py wraps both transparently via `h5py.regionref_dtype`, but verify library version matters for interoperability.

Region references are **same-file only** — the token is an internal identifier, not a portable path.

## Reference Types

| Type | Constant | Points To |
|---|---|---|
| Object reference | `H5R_OBJECT` | An HDF5 object (dataset, group) |
| Region reference | `H5R_REGION` | A selection within a dataset |
| Attribute reference | `H5R_ATTR` | A specific attribute (HDF5 1.12+) |

## h5py API

**Creating a hyperslab region reference**:
`ref = dataset.regionref[start:stop, ...]` — uses numpy-style slice indexing

**Storing references**: Store in a dataset with `dtype=h5py.regionref_dtype`

**Dereferencing to data**: `f[ref]` — returns the selected data as numpy array

**Dereferencing to dataset + selection**:
- `src_dset = f[ref.item()]` — the source dataset
- `src_dset[ref]` — the selected data via the dataset

**Inspecting the selection geometry** (low-level): `h5py.h5r.get_region(ref, dset.id)` returns an `h5py.h5s` dataspace with the selection applied

**Point selections**: h5py's `dataset.regionref[...]` syntax only supports hyperslab (slice) syntax. Point selections require the low-level `h5py.h5s.select_elements()` API.

## Do / Do Not

**Do**:
- Store region references in a dedicated dataset with `dtype=h5py.regionref_dtype`
- Keep references and their target datasets in the same file — cross-file references are not supported
- Document what each reference dataset annotates via attributes (`purpose`, `label_scheme`)
- Use hyperslab references for contiguous rectangular regions; point selections for scattered coordinates

**Do not**:
- Attempt cross-file region references — references encode internal tokens, not portable paths
- Dereference a region reference against a file other than the one it was created in
- Delete a referenced dataset without first removing all references pointing to it — no automatic invalidation
- Store region references in attributes on the referenced dataset itself — circular reference semantics confuse tooling
- Use region references in SWMR mode (restricted operation)
- Hardcode the 12-byte size assumption — use `h5py.regionref_dtype` instead

## Limitations

- **Same-file only**: No cross-file region references
- **File copy caveat**: `h5copy` / `h5repack` may invalidate region references if dataset tokens change — verify after repacking files that contain reference datasets
- **No write-through**: A region reference does not provide write access to the referenced region — dereference to the dataset, then write normally
- **Tool support**: `h5dump -R` decodes region references; most high-level tools (xarray, pandas) ignore them
- **Stale references**: Deleting the referenced dataset leaves references silently invalid — dereferencing returns an error

## Common Pitfalls

**Wrong file for dereference**: `f[ref]` must use the file object of the file containing the target dataset. Using a different opened instance of the same file fails.

**h5repack invalidates references**: After repacking, dataset tokens may change. Re-create or verify references if a workflow repacks intermediate files.

**Point selection via high-level API**: The `dataset.regionref[...]` syntax does not support point selections. Attempting to use a list of indices raises an error — use `h5py.h5s.select_elements()` instead.

**Extended reference size change**: HDF5 1.14+ extended references are 16 bytes vs. 12. Code that serializes reference blobs externally will break across library versions.

## When to Use Region References

- Building an annotation or label layer over large, immutable source datasets
- Storing detected features / ROIs as part of the same HDF5 file as the source data
- Creating index datasets that map identifiers to exact data regions

## When NOT to Use

- Cross-file scenarios — use external links or VDS instead
- Simple rectangular slices where storing offset+shape as integers is equally clear and more portable
- Performance-critical hot paths — dereference adds per-reference overhead
- Files that will be repacked or reorganized frequently

## Citations and References

- [HDF Group: References in HDF5](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_r.html)
- [HDF Group: Object and Region References](https://support.hdfgroup.org/documentation/hdf5-docs/advanced_topics/Intros/References.html)
- [h5py: References Documentation](https://docs.h5py.org/en/stable/refs.html)
- [HDF Group: Extended References (1.14+)](https://docs.hdfgroup.org/hdf5/v1_14/group___h5_r.html)
