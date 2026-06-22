# Tier-1 orchestrator advisory A/B — does the skill change the *agent's* answer?

**Path:** full user-facing `ClioAgent.forward()` (planner -> delegates to HDF5 expert -> skill fires -> answer). **Model:** Groq `gpt-oss-120b`. **Arms:** skill present vs absent.


## Summary
| prompt | route (without -> with) | rubric (without -> with) |
|---|---|---|
| amortization | hdf5 -> hdf5 | 1/4 -> 4/4 |
| consolidation | chat -> hdf5 | 3/3 -> 3/3 |
| when_to_use | chat -> chat | 0/3 -> 0/3 |
| api_usage | chat -> chat | 0/4 -> 0/4 |

**Reading:** the skill demonstrably improves the *agent's* answer **when the planner delegates to the expert** (amortization: 1/4 -> 4/4). Its value is **gated by routing** — for `when_to_use`/`api_usage` the planner answered directly (`chat`), bypassing the expert and the skill, and even hallucinated (e.g. calling clio-core 'columnar', which it is not). `consolidation` showed no delta (the base model's prior was already adequate).


---

## Prompt: `amortization`

> I need to read a large HDF5 file many times in an analysis loop. Should I bundle it into clio-core / IOWarp once so my repeated reads are faster than reading it with h5py each time?


### WITHOUT  (route=`hdf5`, rubric 1/4)

```
**1. What the tool results show**  
The only information supplied is the *hdf5-omni-selective* skill, which describes how IOWarp’s Context Assimilation Engine (CAE) can create an OMNI YAML file that **selectively ingests only the datasets you declare** from an HDF5 file. The skill explains:
- The OMNI YAML schema (src, dst, format, optional `dataset_filter`).
- Filtering rules (exclude first, then optional include patterns).
- The runtime flow: CAE opens the file, walks all objects, applies the filter, and creates CTE tags for the retained datasets.
- No concrete file‑specific metadata (size, chunking, etc.) is present.

**2. What it means (per hdf5-omni-selective)**  
- **Selective loading** can cut both network transfer (for remote files) and memory use if the file contains many large datasets that you never touch.  
- The bundling step itself is **not a caching mechanism**; it is a one‑time transformation that creates tags pointing to the original file’s datasets. Subsequent reads of those tags bypass the HDF5 library’s normal open‑read‑close cycle, but they still incur the same low‑level I/O to the underlying file.  
- If the file resides on a fast local filesystem (SSD/NVMe) and you need to read **the same subset repeatedly**, keeping an open `h5py.File` object across loop iterations (or using HDF5’s internal chunk cache) is usually *faster* than the extra CAE parsing step.  
- If the file is **remote** (e.g., accessed via `hdf5-ros3-vfd` or over HTTP) or you only need a *small fraction* of a multi‑GB file, the OMNI workflow can dramatically reduce data movement and thus overall runtime.

**3. What to do**  
- **Determine the access pattern**:  
  1. List the datasets you actually read in each loop (e.g., via `h5py.File.visititems` or `h5ls -r`).  
  2. Check whether they represent a small subset of the total file.  
- **If you need only a subset**:  
  1. Use the *hdf5-omni-selective* skill to generate an OMNI YAML with explicit `include_patterns` for those datasets (avoid broad includes + excludes).  
  2. Run the CAE (`wrp_cae_omni <omni_file.yaml>`) once before the analysis loop.  
  3. In the loop, read data from the resulting CTE tags (they are thin wrappers; no re‑opening of the original file).  
  4. Verify correctness with a quick `hdf5_visualize_dataset` or a small read benchmark.  
- **If you need most or all datasets** or the file is already local:  
  1. Open the file **once** with `h5py.File('path/to/file.h5', 'r')` before the loop and keep the handle alive.  
  2. Optionally tune the HDF5 chunk cache (`H5Pset_cache`) via h5py’s low‑level API to keep frequently accessed chunks in memory.  
  3. Consider enabling **SWMR** (`h5py.File(..., swmr=True)`) if you have a single writer and many readers, but this is unnecessary for pure read‑only loops.  
- **Performance sanity check**:  
  - Time a single iteration with raw `h5py` reads vs. the OMNI‑tagged reads.  
  - If the OMNI path shows >10 % speedup and memory usage drops, adopt the bundling workflow; otherwise stay with the simple persistent `h5py` handle.

Recommendations:
1. **Profile the loop** – run a few iterations with a persistent `h5py.File` handle and record elapsed time and memory.  
2. **If the loop only touches a small set of datasets** (≤ 10 % of total file size), invoke `hdf5_consult_skill` with `hdf5-omni-selective` (already shown) to craft an OMNI YAML that lists those datasets via `include_patterns`.  
3. Execute `wrp_cae_omni <omni.yaml>` once; then replace the `h5py` reads inside the loop with reads from the generated CTE tags.  
4. **If the loop accesses most of the file** or the file is on a fast local disk, skip OMNI bundling: keep the `h5py.File` open for the whole analysis and, if needed, adjust the HDF5 chunk cache (`h5py.File(...).id.get_create_plist().set_chunk_cache(...)`).  
5. After implementing either path, rerun the benchmark to confirm the expected speed/memory improvement.  

Proceed with the appropriate tool based on the outcome of step 1.
```


### WITH  (route=`hdf5`, rubric 4/4)

```
**Observations from file_context (hdf5‑clio‑core‑ingest skill)**  
- Benchmarking shows **native h5py reads are consistently faster** than reads via clio‑core (CTE) for both cold and warm caches.  
- The per‑object (dataset) penalty for clio‑core is **~11–20 ms** for a full read and **~21–38 ms** just to enumerate objects, versus **~0.1 ms** per dataset for native h5py.  
- Scaling data size (256 MiB → 2 GiB) shows clio‑core warm reads are **3 × slower (cold) to 6–8 × slower (warm)** than native reads; the break‑even point (where ingest‑once pays off) is **never reached**.  
- The dominant cost is **object/chunk count**, not total byte volume. A file with thousands of small datasets incurs minutes of overhead when bundled, while a single large dataset bundles in tens of milliseconds.  
- Bundling can be beneficial **only** for non‑performance reasons (shared‑memory residency, tiered storage, multi‑consumer access) or when native reads are unusually expensive (e.g., highly strided random access). Selective retrieve reduces cost proportionally to the number of requested datasets.

**Interpretation (per hdf5‑clio‑core‑ingest)**  
- For a **large HDF5 file** that you read repeatedly in a loop, the overhead of routing through clio‑core outweighs any theoretical amortization benefit.  
- The performance model is dominated by per‑object latency; unless the file has **very few large datasets**, bundling will increase latency.  
- Consolidating many small datasets into fewer, larger ones can make bundling viable, but that transformation alone already improves native read performance and may eliminate the need for clio‑core altogether.  
- Therefore, the default and optimal path for read‑speed is **direct native access with h5py** (or another low‑level HDF5 API).  

**What to do (summary)**  
- Do **not** bundle the file into clio‑core solely to accelerate repeated reads.  
- Use native h5py reads; if the same data is needed across many processes, consider an in‑memory cache or shared‑memory mapping rather than clio‑core.  
- If the file contains thousands of small datasets and you still need clio‑core for sharing, first **consolidate** those datasets into larger compound structures before ingesting.  
- For selective access patterns, retrieve only the needed subsets via clio‑core’s selective API, but only after the above consolidation step.

Recommendations:
1. **Read the file natively with h5py** in each loop iteration (or cache the loaded arrays in memory if the loop runs within a single process). This yields the lowest latency per the benchmarked TL;DR rule.  
2. **If you must use clio‑core**, first rewrite the HDF5 file to **reduce object count**:
   - Concatenate related small datasets along a new axis or pack them into a single compound/structured dataset.  
   - Verify the new layout with `hdf5_analyze_file` to confirm a small number of large datasets.  
3. **Ingest only after consolidation**, using the CAE assimilator as shown in the skill example, and retrieve **selectively** (`K` datasets) if you need only a subset.  
4. **Validate performance**: run a quick A/B test (`hdf5_analyze_file` + timed reads) comparing native h5py versus clio‑core on the consolidated file before committing to the workflow.  
5. **Do not rely on clio‑core for read‑speed gains**; reserve it for its intended advantages (shared‑memory residency, tiered storage, multi‑consumer scenarios).
```


---

## Prompt: `consolidation`

> I'm about to ingest an HDF5 file with thousands of small datasets into clio-core. Are there performance concerns, and how should I prepare the file?


### WITHOUT  (route=`chat`, rubric 3/3)

```
Ingesting an HDF5 file that contains thousands of tiny datasets can indeed be a performance bottleneck for clio‑core (and for HDF5 in general). The main issues are:

1. **Metadata overhead** – each dataset carries its own header, attributes and possibly its own compression/chunking metadata. Traversing millions of headers slows down file opening, indexing and random access.
2. **I/O inefficiency** – tiny datasets are often stored un‑chunked or with very small chunks, which leads to many small read/write operations and poor disk‑throughput utilization.
3. **Memory pressure** – when the library builds an in‑memory catalog of all objects, the sheer number of entries can consume a lot of RAM.

### Recommended preparation steps

| Step | Why it helps | How to do it (using clio‑core / HDF5 tools) |
|------|--------------|--------------------------------------------|
| **Consolidate** | Reduces the number of objects and metadata look‑ups. | If the logical grouping allows, combine related small arrays into a single larger dataset (e.g., stack them along a new dimension). |
| **Chunk wisely** | Enables efficient block‑wise I/O and better compression ratios. | Use `hdf5_optimize_chunking` (or the HDF5 skill library) to pick a chunk shape that matches your typical access pattern (e.g., read whole rows or columns). For many small 1‑D arrays, a chunk that groups several arrays together (e.g., `(N, M)` where `N` is the number of arrays per chunk) works well. |
| **Apply compression** | Reduces file size and the amount of data transferred from disk. | Choose a fast, loss‑less filter such as `gzip` (level 1‑3) or `blosc`. You can batch‑apply it with `hdf5_apply_filter` or `hdf5_rechunk_dataset`. |
| **Use virtual datasets (VDS)** | Keeps the physical layout compact while presenting a logical view of many small pieces. | Create a VDS that maps the original tiny datasets into a single logical array; this avoids rewriting the data but gives you a single entry point for reading. |
| **Enable SWMR / parallel I/O** (if you will read concurrently) | Prevents lock contention and allows multiple readers without reopening the whole file. | Open the file with the SWMR flag (`h5py.File(..., swmr=True)`) or use the MPI‑I/O driver if you run on a cluster. |
| **Check CF‑conventions** (if the file is meant for scientific sharing) | Guarantees that downstream tools can interpret coordinates, units, etc. | Run `hdf5_check_cf_compliance` to spot missing or malformed attributes. |
| **Profile before ingest** | Gives you concrete numbers on how long indexing takes. | Run `hdf5_analyze_file` to get a summary of dataset count, total size, compression stats, then benchmark a small read with `hdf5_analyze_dataset`. |

### Quick checklist before you hand the file to clio‑core
1. **Run `hdf5_analyze_file`** – note total dataset count and compression summary.
2. **If > 10 000 datasets**, consider consolidating or using a VDS.
3. **Ensure each dataset is chunked** (prefer chunk sizes of at least a few KiB).
4. **Apply a modest compression level** to reduce I/O.
5. **Validate metadata** with `hdf5_check_cf_compliance` if applicable.
6. **Re‑run `hdf5_analyze_file`** after changes to confirm the object count and size have improved.

Following these steps will dramatically reduce ingestion time, lower memory usage during catalog building, and give you a file that is both storage‑efficient and fast to query inside clio‑core.
```


### WITH  (route=`hdf5`, rubric 3/3)

```
**Observations from the bundled skill `hdf5-clio-core-ingest`**

- Ingest‑and‑read cost in clio‑core grows **linearly with the number of objects** (datasets or groups), not with total byte size. Measured overhead is **≈11–20 ms per dataset** for a full read and **≈21–38 ms per dataset** just to enumerate the objects.
- This translates to a **100–200× penalty** compared with native `h5py` reads (≈0.1 ms per dataset). Example timings from the benchmark:
  - 1 000 datasets → native read ≈0.11 s, clio‑core read ≈12.4 s.
  - 8 533 datasets → native read ≈0.9 s, clio‑core read ≈173 s.
- The per‑object penalty is **independent of dataset size**; large contiguous datasets incur only a modest constant factor (≈3× cold, ≈6–8× warm) compared to native reads.
- **Bundling does not accelerate reads**. Even for a single 2 GB contiguous dataset, clio‑core warm read is slower than a cold native read. The “break‑even” point (reads saved after a one‑time ingest cost) is effectively **never** unless the native read is unusually slow (e.g., heavy random/strided access) and the file will be read many times.
- **Selective retrieval** (asking for a subset of datasets) scales with the number requested (K) rather than total N, so retrieving 10 % of a many‑object file is ≈10× cheaper than retrieving all objects.
- The ingest API expects routing via the `src` prefix (`hdf5::` for per‑dataset ingest, `file::` for opaque binary). Mis‑using `format=` does not affect routing.

**Interpretation (per `hdf5-clio-core-ingest`)**

- **Performance concern:** the dominant factor is the **high object count**. Thousands of tiny datasets will cause minutes‑scale ingest‑and‑read latency in clio‑core.
- **Root cause:** clio‑core stores each dataset as a separate blob (≈1.5 MiB chunks). The per‑blob round‑trip dominates the runtime.
- **Mitigation strategy:** **reduce the object count** before ingesting—consolidate many small datasets into fewer larger ones (e.g., concatenate along a new axis, pack into a table, or use a compound dataset). After consolidation, per‑object overhead drops dramatically and clio‑core performance becomes comparable to native reads.
- If consolidation is impossible, the recommendation is to **skip clio‑core ingest** and read the file directly with `h5py`, reserving clio‑core only for its sharing/ tiering features, not for speed.

Recommendations:
1. **Inspect the file first**  
   ```bash
   hdf5_analyze_file path/to/file.h5
   ```  
   Verify the total number of datasets/groups. If the count is > ~500, treat it as a “many‑small‑objects” case.

2. **Consolidate small datasets**  
   - Create a new HDF5 file where related small datasets are stacked into a single dataset (e.g., `np.concatenate([...], axis=0)`), or pack them into a compound/table dataset.  
   - Use the HDF5 API (`h5py` or `h5repack`) to write the consolidated file.  
   - Optionally, after consolidation, **re‑chunk** the new large dataset for your access pattern (invoke `hdf5_rechunk_dataset` if you need a different chunk shape). Consult the `hdf5-chunking` skill for optimal chunk size calculations.

3. **Re‑run the inspection** on the consolidated file to confirm that the object count is now low (ideally < 50).  

4. **Ingest with the correct routing** (only if you need clio‑core’s shared‑memory or tiering benefits):  
   ```python
   import clio_cee, clio_cte_core_ext as cte
   ci = clio_cee.ContextInterface()
   ctx = clio_cee.AssimilationCtx(
           src=f"hdf5::{consolidated_path}",
           dst=f"iowarp::{tag}",
           format="hdf5")   # format is ignored; src prefix drives routing
   ci.context_bundle([ctx])   # returns 0 on success
   ```
   - Use `src="hdf5::"` for per‑dataset (semantic) ingest; avoid `src="file::"` unless you deliberately want opaque binary chunking.

5. **Read back selectively** (if you only need a subset of the original small datasets):  
   - Retrieve only the needed blobs via the CTE tag API (`Tag.GetBlob`).  
   - This scales with the number of requested datasets, not the total N (per `hdf5-clio-core-ingest`).

6. **If consolidation cannot be performed** (e.g., datasets are semantically independent and must stay separate), **do not ingest**. Use native `h5py` reads, which are orders of magnitude faster for many small objects.

7. **Optional validation** – run a quick A/B benchmark:
   ```bash
   # native read
   time python -c "import h5py; f=h5py.File('orig.h5','r'); list(f.keys())"
   # clio‑core read (after ingest)
   time python -c "import clio_cte_core_ext as cte; t=cte.Tag('tag/dataset'); t.GetBlob(... )"
   ```
   Confirm that the native path is faster before committing to the ingest workflow.

**Next step (if you need concrete chunking guidance)**: invoke the skill `hdf5-chunking` to obtain recommended chunk shapes for the consolidated dataset based on your typical read/write access pattern.
```


---

## Prompt: `when_to_use`

> When is it actually worth ingesting an HDF5 file into clio-core instead of just reading it with h5py?


### WITHOUT  (route=`chat`, rubric 0/3)

```
Ingesting an HDF5 file into clio‑core (i.e., converting it into Clio’s internal columnar/metadata format) makes sense when you need any of the following capabilities that go beyond what a simple h5py read provides:

1. **Fast, column‑oriented queries and sub‑setting** – Clio stores data in a columnar layout with indexes, enabling rapid slicing, filtering, and projection without loading entire datasets into memory. If you repeatedly query subsets, filter on attributes, or join across multiple datasets, ingestion pays off.

2. **Cross‑file analytics and unified metadata** – Clio‑core can combine HDF5 data with other supported formats (Parquet, CSV, etc.) under a single schema, making it easier to run analytics pipelines that span heterogeneous sources.

3. **Advanced compression & chunk‑optimisation** – During ingestion Clio can re‑chunk and re‑compress datasets using optimal filters (e.g., Zstandard, Blosc) tailored to your access patterns, often yielding smaller on‑disk size and better I/O performance than the original HDF5 layout.

4. **Built‑in statistics & profiling** – Clio automatically computes column statistics (min/max, histograms, null counts) that are stored alongside the data, enabling quick data profiling without scanning the raw file each time.

5. **Versioning, lineage, and auditability** – Ingested datasets become first‑class assets in Clio’s catalog, supporting version control, provenance tracking, and reproducible pipelines.

6. **Scalable downstream processing** – Once in Clio’s format, the data can be processed by Clio’s distributed execution engine, Spark‑like query planners, or visualisation tools without the overhead of custom h5py code.

7. **Cloud‑optimized access** – Clio can store the ingested data in cloud‑friendly object stores (e.g., S3) with range‑byte‑addressable reads, whereas raw HDF5 files often require whole‑file downloads for random access.

**When you probably don’t need ingestion**:
- One‑off, simple reads where you load an entire dataset into memory.
- Light‑weight scripts that just need a few values or small slices.
- Situations where you must preserve the exact original HDF5 structure (e.g., for downstream tools that expect native HDF5 files).

In short, ingest into clio‑core when you need high‑performance, column‑oriented analytics, cross‑format integration, advanced compression, or catalog‑level features; otherwise, a direct h5py read is sufficient.
```


### WITH  (route=`chat`, rubric 0/3)

```
Ingesting an HDF5 file into clio‑core is worthwhile when you want to take advantage of clio‑core’s higher‑level data‑management features that go beyond what h5py provides. Specifically, consider ingestion when you need:

1. **Schema discovery & cataloging** – clio‑core can automatically index datasets, groups, attributes, and compression details, making it easy to query the file’s contents without opening the file each time.
2. **Cross‑file querying & federation** – once ingested, the file’s metadata can be joined with other datasets in the clio catalog, enabling unified SQL‑like queries across heterogeneous sources.
3. **Versioning & provenance tracking** – clio‑core records when and how a file was ingested, supporting reproducible pipelines and audit trails.
4. **Optimized I/O & chunk‑layout recommendations** – the ingestion process can analyze access patterns and suggest (or apply) better chunking/compression, which h5py alone would not do automatically.
5. **Integration with downstream tools** – clio‑core exposes a uniform API that downstream analysis, visualization, or machine‑learning components can consume without needing to know HDF5 specifics.
6. **Metadata validation & CF‑convention checks** – clio‑core can run built‑in compliance checks (e.g., CF conventions) during ingestion, flagging issues early.
7. **Persistent caching** – frequently accessed subsets can be cached in a format better suited for rapid reads (e.g., Parquet) as part of the ingestion workflow.

If you only need to read a few datasets, perform simple slicing, or run quick ad‑hoc analyses, h5py remains the lightweight choice. Ingest into clio‑core makes sense for larger, production‑scale workflows where metadata management, queryability, reproducibility, and I/O optimization provide measurable benefits.

**Bottom line:** Use clio‑core ingestion when you need catalog‑level metadata, cross‑dataset queries, versioning, automated optimization, or compliance checks; otherwise, stick with direct h5py reads for quick, low‑overhead access.
```


---

## Prompt: `api_usage`

> How do I ingest an HDF5 file into clio-core and then read one dataset's array back out?


### WITHOUT  (route=`chat`, rubric 0/4)

```
To ingest an HDF5 file into **clio‑core** and then read a specific dataset back out, follow these steps:

1. **Make sure clio‑core is installed**
   ```bash
   pip install clio-core
   ```

2. **Ingest the HDF5 file**
   Use the `clio ingest` command, pointing it at your HDF5 file. By default clio‑core will catalog the file and its internal objects (datasets, groups, attributes) in its internal metadata store.
   ```bash
   clio ingest /path/to/your_file.h5
   ```
   - You can add `--metadata` flags if you want to attach extra tags.
   - The command will print a short summary, e.g. the number of datasets discovered.

3. **List the datasets that were ingested** (optional, but useful to get the exact object path)
   ```bash
   clio list-datasets /path/to/your_file.h5
   ```
   The output will look like:
   ```
   /group1/datasetA   shape=(100, 200)   dtype=float32
   /group2/datasetB   shape=(50,)        dtype=int64
   ```
   Pick the dataset you want to read, e.g. `/group1/datasetA`.

4. **Read the dataset’s array back into Python**
   You have two common ways:
   - **Using the clio‑core Python API**
     ```python
     from clio import Clio

     # Open a Clio session (it will locate the internal catalog automatically)
     cl = Clio()

     # Load the dataset as a NumPy array
     array = cl.read_dataset(
         filepath="/path/to/your_file.h5",
         object_path="/group1/datasetA"
     )

     print(array.shape, array.dtype)
     ```
   - **Using the CLI to dump to a temporary file**
     ```bash
     clio export /path/to/your_file.h5 /group1/datasetA --format numpy --output /tmp/datasetA.npy
     ```
     Then in Python you can load it with `np.load('/tmp/datasetA.npy')`.

5. **Verify the data (optional)**
   ```python
   import numpy as np
   print(np.mean(array))
   print(np.min(array), np.max(array))
   ```

### Quick reference of the key commands
| Action | CLI command | Python API |
|--------|-------------|------------|
| Ingest HDF5 | `clio ingest <file.h5>` | `cl.ingest(filepath)` |
| List datasets | `clio list-datasets <file.h5>` | `cl.list_datasets(filepath)` |
| Read a dataset | `clio export <file.h5> <object_path> --format numpy --output <out.npy>` | `cl.read_dataset(filepath, object_path)` |

**Notes**
- The `object_path` is the full HDF5 internal path (including leading `/`).
- If the file is large and you only need a slice, the API also supports `slice` arguments, e.g. `cl.read_dataset(..., slice=(slice(0,10), slice(None)))`.
- clio‑core automatically detects compression filters (gzip, szip, etc.) and will decompress on the fly, so you don’t need to handle that yourself.

That’s it! After ingestion, any dataset inside the HDF5 file can be accessed via the CLI or the Python API as shown above.
```


### WITH  (route=`chat`, rubric 0/4)

```
To ingest an HDF5 file into clio‑core and then read a specific dataset’s array back out, follow these steps:

1. **Place the HDF5 file where clio‑core can see it**
   - Copy or move your `.h5`/`.hdf5` file into a directory that is part of the clio‑core data path (e.g., `data/` in your project).  You can use the filesystem tools (`fs_apply_edit_write` or `fs_propose_edit`) if you need to create or modify files programmatically.

2. **Register the file with clio‑core**
   - In a Python session (or a Jupyter notebook) import the core library and add the file to the catalog:
   ```python
   from clio_core import Catalog

   catalog = Catalog()
   # Register the HDF5 file; the path is relative to the project root or absolute.
   catalog.register_file('my_data.h5')
   ```
   - This step tells clio‑core to index the file and make its contents discoverable.

3. **Discover the datasets inside the file**
   - Use the HDF5‑specific helper to list datasets:
   ```python
   from clio_core.hdf5 import list_datasets

   datasets = list_datasets('my_data.h5')
   print(datasets)  # each entry includes name, shape, dtype, size
   ```
   - Choose the dataset you want to read, e.g., `'/temperature'`.

4. **Read the dataset into a NumPy array**
   - clio‑core provides a thin wrapper around `h5py` for reading:
   ```python
   from clio_core.hdf5 import read_dataset
   import numpy as np

   arr: np.ndarray = read_dataset('my_data.h5', '/temperature')
   print(arr.shape, arr.dtype)
   ```
   - `read_dataset` opens the file, accesses the object path you supplied, and returns the full array (or a slice if you pass additional slicing arguments).

5. **(Optional) Verify the data**
   - You can call the built‑in analysis tool to confirm the dataset’s metadata:
   ```python
   from clio_core.hdf5 import analyze_dataset
   meta = analyze_dataset('my_data.h5', '/temperature')
   print(meta)
   ```
   - The output includes shape, dtype, compression, chunk layout, and basic statistics (min, max, mean, etc.).

6. **(Optional) Persist the array elsewhere**
   - If you need the data in another format, you can write it out with standard NumPy or pandas utilities, e.g.:
   ```python
   np.save('temperature.npy', arr)          # NumPy binary
   import pandas as pd
   pd.DataFrame(arr).to_csv('temperature.csv', index=False)
   ```

### Summary of the key clio‑core calls
| Goal | clio‑core function | Description |
|------|-------------------|-------------|
| Register file | `Catalog().register_file(path)` | Indexes the HDF5 file for later queries |
| List datasets | `list_datasets(path)` | Returns a list of dataset names with shapes/dtypes |
| Read a dataset | `read_dataset(path, object_path)` | Returns a NumPy array of the full dataset |
| Inspect metadata | `analyze_dataset(path, object_path)` | Provides shape, dtype, compression, chunking, stats |

These steps give you a complete ingestion‑to‑read workflow using clio‑core’s HDF5 support. If you encounter any issues (e.g., missing dependencies, permission errors, or need to apply a filter/compression), let me know and we can dive into the relevant tool (e.g., `hdf5_apply_filter` or `hdf5_check_compression`).

**Reason**: The user asked for a procedural guide; no file‑specific facts are required, so we can answer directly.
```
