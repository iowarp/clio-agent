---
name: hdf5-clio-core-ingest
description: "ingest hdf5 into clio-core", "bundle into cte", "context_bundle", "should i use clio-core", "consolidate small datasets", "many small datasets slow", "amortize ingest", "iowarp ingest performance", "cae assimilator", "bundle vs native read", "context_retrieve", when and how to ingest HDF5 files into clio-core (CTE blob store via the CAE assimilator) and when NOT to — consolidation and amortization rules of thumb from benchmarking.
version: 0.1.0
---

# Ingesting HDF5 into clio-core (CTE/CAE)

## Purpose

Decide **whether** to route an HDF5 file through clio-core's blob store
(ingest via the CAE assimilator = `context_bundle`, read back via the CTE data
plane) instead of reading it natively with h5py — and **how** to do it without
falling into the slow paths. Guidance is grounded in benchmarking on the
dev-container build (functional/relative comparison, not absolute GB/s).

**Related skills**: `hdf5-chunking` for native layout, `hdf5-io` for write
buffering, `hdf5-omni-selective` for selective assimilation.

## TL;DR decision rule

- **Default: read natively with h5py.** Bundling into clio-core is *not* a
  read-latency optimization — across every fixture tested, CEE read latency is
  higher than native h5py (2–8× warm; never lower than even a *cold* native
  read). Reach for clio-core for its *other* properties (shared-memory residency
  for many concurrent consumers, multi-tier placement), not to make a single
  client's reads faster.
- **If you do ingest: consolidate first.** The cost is per-*object*, not
  per-byte. Many small datasets are catastrophic; few large datasets are fine.

## Practice 1 — Consolidate small datasets before ingest (biggest lever)

Ingest + retrieve cost scales **linearly with dataset (object) count**, at a
near-constant **~11–20 ms per dataset** for a full read and **~21–38 ms per
dataset** to enumerate — versus ~0.1 ms/dataset for native h5py. That is a
**~100–200× per-object penalty**, measured across 10 → 8,533 datasets:

| datasets | native read | clio-core read | penalty |
|---:|---:|---:|---:|
| 10 | 1.3 ms | 115 ms | ~90× |
| 100 | 11 ms | 1.1 s | ~100× |
| 1,000 | 112 ms | 12.4 s | ~110× |
| 8,533 | 0.9 s | 173 s | ~196× |

**Action:** before bundling a file with thousands of small datasets, consolidate
them into fewer, larger datasets (concatenate along a new axis, or pack into one
table). A file that is one big dataset bundles in tens of ms; the same bytes
spread over thousands of datasets take minutes. If you cannot consolidate,
prefer native h5py.

## Practice 2 — Don't bundle to make reads faster (it doesn't amortize)

The intuition "pay ingest once, then cheap reads forever" does **not** hold here.
Break-even N (reads before ingest pays off) was **"never" at every scale** —
clio-core's warm read is slower than a *cold* native read even for large
contiguous data:

| dataset size | native cold | native warm | clio-core warm | break-even |
|---:|---:|---:|---:|---:|
| 256 MB | 293 ms | 61 ms | 396 ms | never |
| 1 GB | 1.1 s | 225 ms | 1.55 s | never |
| 2 GB | 2.1 s | 398 ms | 3.2 s | never |

The cost is **per-chunk** (CTE re-chunks at 1.5 MB → a 2 GB dataset is ~1,400
blobs, each a round-trip). **Action:** justify bundling by sharing/tiering
needs, not read speed. The only situation where bundling can win is when the
*native* read is itself unusually expensive (e.g. heavily strided/random
access) **and** the file is re-read many times — verify with a quick A/B before
committing to it.

## Practice 3 — Use selective retrieve for subsets

If you need a subset of datasets, retrieve only those — retrieve cost scales
with the number requested (K), not the file total (N). Selecting ~10% of a
many-dataset file cost ~10× less than retrieving all of it. **Action:** filter
by tag/blob pattern at retrieve time rather than pulling the whole bundle.
(Ingest still reads the whole file — ingest-side dataset filtering is not yet
exposed in the Python API.)

## Practice 4 — Per-byte scaling is fine; it's objects/chunks that hurt

clio-core scales gracefully with data *size* (read overhead is a constant ~3×
cold / ~6–8× warm from 256 MB to 2 GB) but badly with object/chunk *count*.
**Action:** large contiguous datasets are the friendly case; high object counts
are the hostile one. Optimize the file's structure (fewer objects), not its
total size.

## How to ingest and read correctly (API gotchas)

```python
import clio_cee, clio_cte_core_ext as cte
import numpy as np

ci = clio_cee.ContextInterface()
# Routing is by the src PROTOCOL PREFIX, not the format field:
#   hdf5::  -> semantic per-dataset ingest (one tag per dataset)
#   file::  -> opaque whole-file byte chunking
ctx = clio_cee.AssimilationCtx(src=f"hdf5::{path}", dst=f"iowarp::{tag}",
                               format="hdf5")  # format= is IGNORED for routing
assert ci.context_bundle([ctx]) == 0

# Read a dataset back via the CTE data plane (NOT context_retrieve):
t = cte.Tag(f"{tag}/{dataset_path}")           # e.g. "{tag}/field"
chunks = sorted((b for b in t.GetContainedBlobs() if b.startswith("chunk_")),
                key=lambda s: int(s.split("_")[1]))   # numeric order matters!
buf = b"".join(t.GetBlob(c, t.GetBlobSize(c), 0) for c in chunks)
arr = np.frombuffer(buf, dtype=dtype).reshape(shape)   # dtype/shape from the
                                                       # tensor<...> description blob
```

## Best Practices

- Default to native h5py; bundle into clio-core only for sharing/tiering, not read speed.
- Consolidate many small datasets into few large ones before ingest.
- Retrieve subsets selectively (cost ∝ K requested, not N total).
- Read binary back via CTE `Tag.GetBlob` (returns `bytes`); order `chunk_N` by numeric index.
- Use `src="hdf5::"` for semantic ingest; treat `format=` as cosmetic.

## Common Pitfalls

- **`src="file::"` with `format="hdf5"`** → silently gets the opaque binary
  chunker (no per-dataset structure). Routing is the `src` prefix only.
- **Using CEE `context_retrieve` for array data** → it is the text/LLM-context
  layer (UTF-8) and raises `UnicodeDecodeError` on binary. Use CTE `Tag.GetBlob`.
- **Bundling a many-small-dataset file as-is** → minutes of per-object overhead.
  Consolidate, or don't bundle.
- **Expecting attributes / hyperslab reads** → the assimilator ingests whole
  datasets only and does not read HDF5 attributes.
