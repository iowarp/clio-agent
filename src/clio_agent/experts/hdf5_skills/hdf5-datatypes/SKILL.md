---
name: hdf5-datatypes
description: "compound datatype", "variable-length string", "enum type", "opaque type", "structured array", "numpy structured dtype", "H5T_COMPOUND", "HDF5 string encoding", "vlen string", "HDF5 custom types", "nested struct HDF5", "array datatype", "variable-length sequence"
version: 0.1.0
---

# HDF5 Compound & Complex Datatypes

## Purpose

HDF5 supports a rich type system beyond numeric arrays: compound types (struct-like records), variable-length strings, enumerations, opaque, and array types. This skill covers their correct use, numpy mapping, and common failure modes.

**Related skills**: `hdf5-filters` for compression (compound types are filterable; VL types are not), `hdf5-io` for write performance.

## Type System Overview

| HDF5 Type | Numpy Analog | h5py Creation |
|---|---|---|
| `H5T_COMPOUND` | structured array / `np.dtype([...])` | Pass structured dtype to `create_dataset` |
| `H5T_STRING` fixed | `'S10'` bytes | `h5py.string_dtype(length=N)` or `'SN'` |
| `H5T_STRING` variable | Python str | `h5py.string_dtype()` (length=None) |
| `H5T_ENUM` | int + lookup dict | `h5py.enum_dtype(dict, basetype)` |
| `H5T_OPAQUE` | raw bytes | `h5py.opaque_dtype(nbytes)` |
| `H5T_ARRAY` | fixed sub-array per element | `h5py.array_dtype(basetype, shape)` |
| `H5T_VLEN` | ragged sequence | `h5py.vlen_dtype(basetype)` |

## Compound Types

Compound types store structured records — C structs or numpy structured arrays. Each field has a name, HDF5 type, and byte offset.

**In h5py**: Pass a numpy structured dtype directly to `create_dataset`. h5py maps it to `H5T_COMPOUND` automatically.

**Key rules**:
- Field names are case-sensitive and unique
- Nested compound types (struct-of-structs) are supported
- Numpy may add alignment padding that does not match the HDF5 field offsets — use `{'names': [...], 'formats': [...], 'offsets': [...], 'itemsize': N}` when round-tripping with C structs
- Reading returns a numpy structured array; individual fields accessed as `data['fieldname']`
- **Partial I/O**: HDF5 supports reading only a subset of compound fields (memory type differs from file type). h5py does not expose this directly; use `h5py.h5t` low-level API to construct partial memory types

## String Types

### Fixed-Length Strings
- Null-padded or null-terminated byte arrays — portable, predictable storage size
- Use `h5py.string_dtype(length=N)` or `dtype='SN'`
- Always read back as `bytes` in Python 3 — decode explicitly

### Variable-Length Strings
- Each element is a heap pointer — convenient, but cannot be compressed (bypasses chunk filters)
- Use `h5py.string_dtype()` for variable length
- Prefer `h5py.string_dtype(encoding='utf-8')` for international text
- Read back as `str` in h5py ≥ 3.x; as `bytes` in h5py 2.x — code that auto-decodes one breaks on the other
- **SWMR restriction**: VL string datasets cannot be modified in SWMR mode

**Recommendation**: Fixed-length strings for performance-critical or archival data; VL strings only for human-readable metadata where length is genuinely unpredictable.

## Enumeration Types

Maps named labels to an integer base type — useful for categorical data (sensor states, status codes).

`h5py.enum_dtype({'OFF': 0, 'ON': 1, 'FAULT': 2}, dtype=np.uint8)`

- Storage is the base integer type — compact and filterable
- h5py reads enum fields back as plain integers; retrieve the enum dict via `dataset.id.get_type().get_enum_dict()`
- For portability to non-h5py readers, store the enum mapping in attributes alongside the dataset

## Opaque Types

Raw byte sequences with no HDF5-interpreted structure. Fixed size per element.

`h5py.opaque_dtype(nbytes)`

- Cannot be filtered — HDF5 cannot interpret the bytes
- Loses interoperability with any tool other than your own code
- **Preferred alternative**: store as a fixed-length bytes dataset with a `format` attribute documenting the encoding

## Array Types (H5T_ARRAY)

Fixed-shape sub-arrays embedded in each element. A dataset of shape `(N,)` with type `H5T_ARRAY[3][3]` of float reads as shape `(N, 3, 3)`.

`h5py.array_dtype(np.float64, (3, 3))`

- More compact than a compound type with 9 float fields for the same data
- Chunking applies to outer dataset dimensions only — cannot filter on sub-array dimensions

## Variable-Length Sequences (H5T_VLEN)

Each element is a variable-length sequence of a base type stored on the heap.

`h5py.vlen_dtype(np.int32)`

- Reads as a numpy object array (ragged) — no vectorized operations
- **Cannot be compressed** — heap storage bypasses filters
- High per-element overhead; avoid for large uniform arrays
- Use only when element lengths are genuinely irregular and total count is small
- **C API**: must call `H5Dvlen_reclaim()` to free heap memory; h5py handles this automatically

## Do / Do Not

**Do**:
- Match field offsets explicitly when round-tripping compound types to/from C structs
- Set string encoding explicitly (`utf-8`) for any file shared outside your team
- Store enum dictionaries in attributes for portability to non-h5py readers
- Use fixed-length strings inside compound types — VL strings in compounds are legal but complex

**Do not**:
- Use VL strings or VL sequences in performance-critical datasets — heap allocation is slow and kills compression
- Expect VL types to compress — they will not, regardless of filter settings
- Use opaque types as a catch-all for hard-to-model data — the interoperability cost is rarely worth it
- Assume h5py 2.x and 3.x decode strings the same way — test explicitly

## Common Pitfalls

**Alignment mismatch**: `np.dtype` with `align=True` may produce different itemsize/offsets than the HDF5 compound. Verify with `dtype.itemsize` vs. the HDF5 type's `get_size()`.

**Enum round-trip**: h5py does not auto-convert integer reads to named enum values. Retrieve and apply the enum dict manually.

**VL memory leak in C**: C code reading VL data must call `H5Dvlen_reclaim()`. Forgetting this leaks heap memory silently.

**Partial compound read**: Reading a subset of compound fields requires constructing a custom memory datatype via the low-level API — the high-level `dataset[...]` always reads all fields.

## Citations and References

- [HDF Group: HDF5 Datatypes](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_t.html)
- [h5py: Special Datatypes](https://docs.h5py.org/en/stable/special.html)
- [HDF Group: Strings in HDF5](https://support.hdfgroup.org/documentation/hdf5-docs/advanced_topics/UsingUnicode.html)
- [HDF Group: Compound Datatypes Tutorial](https://support.hdfgroup.org/documentation/hdf5/latest/_learn_basics_compound.html)
