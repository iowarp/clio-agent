# HDF5 Expert × `hdf5-clio-core-ingest` skill — A/B demonstration (advice quality)

**Setup:** clio-agent HDF5Expert, skill-synthesis path (no file path in prompt). Model: `granite3.1-dense:8b` via Ollama, temperature 0, deterministic. **Without** = skill dir moved outside the bundle (expert falls back to a tangential skill); **With** = skill present. Rubric = empirically-correct claims from the clio-core benchmark; it understates the gap (off-topic 'without' answers occasionally hit a keyword). The response text is the real evidence.


## Summary

| prompt | without (skill / score) | with (skill / score) |
|---|---|---|
| amortization | hdf5-omni-selective — 1/4 | hdf5-clio-core-ingest — 2/4 |
| consolidation | hdf5-core-vfd — 2/3 | hdf5-clio-core-ingest — 3/3 |
| api_usage | hdf5-swmr — 0/4 | hdf5-clio-core-ingest — 2/4 |

---

## Prompt: `amortization`

> I need to read a large HDF5 file many times in an analysis loop. If I bundle it into clio-core / IOWarp once, will my repeated reads then be faster than reading the file with h5py each time?


### WITHOUT skill  (injected: `hdf5-omni-selective`, rubric 1/4)

_rubric:_ ✗ says NOT faster, ✗ no amortization / break-even, ✓ prefer native h5py for speed, ✗ value is sharing/tiering not speed

```
The provided file context describes an OMNI YAML configuration for selective HDF5 data assimilation using IOWarp. This approach allows users to specify exactly which datasets are needed from an HDF5 file, avoiding unnecessary data transfer and improving efficiency. The key aspects of this method include:

1. **Dataset Filtering**: Using glob patterns in the OMNI YAML file to selectively include or exclude datasets based on their paths.
2. **Include Patterns**: These patterns determine which datasets are loaded into CTE tags during runtime, ensuring that only necessary data is transferred.
3. **Exclude Patterns**: These patterns can be used to exclude specific datasets from being loaded, further refining the selection process.
4. **Validation**: It's essential to verify that the include patterns match the actual dataset paths in the HDF5 file using tools like `h5ls`.
Given your requirement to read a large HDF5 file many times within an analysis loop, bundling it into an OMNI YAML configuration and assimilating it with IOWarp can indeed lead to faster repeated reads. Here's how you can proceed:

1. **Identify Required Datasets**: First, determine which datasets are necessary for your analysis by inspecting the HDF5 file structure using `h5py` or the CLI command `h5ls -r`.
2. **Create an OMNI YAML File**: Based on the identified datasets, create an OMNI YAML configuration with tight include patterns to specify only the required data. This step ensures that unnecessary data is not transferred, improving efficiency.
3. **Assimilate Data with IOWarp**: Use IOWarp's Context Assimilation Engine (CAE) to parse the OMNI YAML file and load the specified datasets into CTE tags during runtime. This process filters out unwanted datasets based on the include/exclude patterns defined in the YAML configuration.
4. **Validate Dataset Paths**: Ensure that the include patterns match the actual dataset paths in the HDF5 file using `h5ls -r`.
5. **Run Assimilation**: Execute the assimilation via the command `wrp_cae_omni <omni_file.yaml>`.

By following these steps, you can significantly improve the performance of your repeated reads by avoiding unnecessary data transfer and leveraging IOWarp's selective dataset loading capabilities.
```


### WITH skill  (injected: `hdf5-clio-core-ingest`, rubric 2/4)

_rubric:_ ✗ says NOT faster, ✗ no amortization / break-even, ✓ prefer native h5py for speed, ✓ value is sharing/tiering not speed

```
The provided file context indicates that ingesting an HDF5 file into clio-core (CTE blob store via the CAE assimilator) is not recommended for improving read latency. In fact, it results in a significant performance penalty of 100-200x per dataset compared to native h5py reads. This penalty scales linearly with the number of datasets and is due to the overhead of managing many small objects in clio-core.

The skill `hdf5-clio-core-ingest` provides a decision rule: "Default: read natively with h5py." It suggests consolidating small datasets into larger ones before ingestion, as this can significantly reduce the per-object penalty. However, even after consolidation, clio-core's warm read latency is still higher than native h5py reads across all tested scales.
Given the analysis, it is recommended to read the HDF5 file natively with h5py for your analysis loop that requires many repeated reads. If you still wish to use clio-core for other reasons (e.g., shared-memory residency or multi-tier placement), consider consolidating small datasets into larger ones before ingestion to minimize the per-object penalty.

To implement this recommendation, you can use the following steps:
1. Consolidate small datasets into larger ones using appropriate methods (e.g., concatenating along a new axis or packing into one table).
2. Read the consolidated HDF5 file natively with h5py in your analysis loop.

If you decide to proceed with clio-core ingestion despite the performance penalty, remember to use the correct routing protocol prefix (`hdf5::` for semantic per-dataset ingest) and read binary data back via the CTE data plane using `Tag.GetBlob`. Avoid using `context_retrieve` for array data, as it raises a `UnicodeDecodeError` on binary content.
```


---

## Prompt: `consolidation`

> I'm about to ingest an HDF5 file with thousands of small datasets into clio-core. Are there performance concerns, and how should I prepare the file?


### WITHOUT skill  (injected: `hdf5-core-vfd`, rubric 2/3)

_rubric:_ ✗ consolidate / fewer larger datasets, ✓ per-object / object-count cost, ✓ cost magnitude (slow/overhead/x)

```
The file context provided indicates that you are planning to use an HDF5 file with thousands of small datasets in clio-core. The bundled HDF5 skill (hdf5-core-vfd) is a memory file driver, which maintains HDF5 files entirely in RAM rather than on disk. This approach eliminates file system I/O overhead for temporary data operations and high-performance scenarios.

The primary use cases for this VFD include I/O buffer applications, temporary/scratch space, baseline performance testing, delayed storage decisions, fast prototyping and testing, and network transmission of HDF5 data. However, there are some potential concerns to consider:

1. **Memory constraints**: The file size is limited by available system RAM. If the dataset is larger than your system memory, this approach may not be suitable.
2. **No persistence without backing store**: Data will be lost when the file is closed if `backing_store=False`.
3. **Single process**: Core VFD is not designed for concurrent access by multiple processes; use an appropriate parallel VFD instead.
4. **Entire file in memory**: Even small accesses require the full file to be loaded into memory, which may not be efficient for large files with only small portions accessed.
Given these considerations, here are some recommendations:

1. **Use backing_store=True** if you need persistent storage without explicitly managing it. This will write changes to disk on `close()` or `flush()`.
2. **Set an appropriate block_size**: Larger increments (1-10 MB) for files expected to grow significantly can reduce reallocation overhead.
3. **Prefer File.in_memory()** for purely in-memory files, as it provides a cleaner API and fewer edge cases.
4. **Use for testing and development**: This approach is fast and does not require cleanup or isolation from file system state.
5. **Profile memory usage**: Monitor RAM consumption for large in-memory files to avoid exhausting available memory.
6. **Consider write tracking** when using backing store with selective modifications, as it optimizes disk writes by recording only modified regions.
7. **Load read-only files into memory** for repeated access, as this is faster than repeated disk reads.
8. **Avoid using Core VFD** for files larger than available system memory, persistent storage without explicit backing store management, concurrent access by multiple processes, long-running applications with limited RAM, or large files where only small portions are accessed.
```


### WITH skill  (injected: `hdf5-clio-core-ingest`, rubric 3/3)

_rubric:_ ✓ consolidate / fewer larger datasets, ✓ per-object / object-count cost, ✓ cost magnitude (slow/overhead/x)

```
The provided file context indicates that ingesting an HDF5 file with thousands of small datasets into clio-core may lead to performance issues. The hdf5-clio-core-ingest skill emphasizes that the cost of ingest and retrieve operations scales linearly with dataset (object) count, resulting in a significant per-object penalty (~100–200×). This is due to clio-core's design, which prioritizes shared-memory residency for many concurrent consumers and multi-tier placement over read latency optimization.

The skill also highlights that bundling into clio-core does not amortize ingest costs, as the warm read is slower than a cold native read even for large contiguous data. The break-even point (N reads before ingest pays off) was never reached at every scale tested.

Moreover, the skill suggests that per-byte scaling is fine; it's objects/chunks that hurt. Clio-core scales gracefully with data size but badly with object/chunk count. Large contiguous datasets are the friendly case, while high object counts are hostile.
1. Consolidate small datasets into fewer, larger datasets before ingestion to minimize the per-object penalty (~100–200×). This can be achieved by concatenating along a new axis or packing into one table.
2. If consolidation is not possible, consider using native h5py for reading instead of bundling into clio-core.
3. When retrieving subsets, use selective retrieve (cost ∝ K requested, not N total) to minimize the number of objects read back.
4. Read binary data back via CTE `Tag.GetBlob` (returns bytes); order chunks by numeric index.
5. Use `src="hdf5::"` for semantic ingest; treat `format=` as cosmetic.
6. Avoid using `src="file::"` with `format="hdf5"` to prevent silent use of the opaque binary chunker without per-dataset structure.
7. Do not use CEE `context_retrieve` for array data, as it is designed for text/LLM-context (UTF-8) and raises `UnicodeDecodeError` on binary data. Instead, use CTE `Tag.GetBlob`.
8. Be aware that bundling a many-small-dataset file as-is may result in minutes of per-object overhead; consolidate or avoid bundling if possible.
9. Note that the assimilator ingests whole datasets only and does not read HDF5 attributes, so expecting attribute or hyperslab reads will not be supported.
```


---

## Prompt: `api_usage`

> How do I ingest an HDF5 file into clio-core and then read one dataset's array back out?


### WITHOUT skill  (injected: `hdf5-swmr`, rubric 0/4)

_rubric:_ ✗ hdf5:: routing (src prefix), ✗ context_bundle for ingest, ✗ CTE Tag.GetBlob for read, ✗ avoid context_retrieve for binary

```
To ingest data from a file using SWMR (Single Writer Multiple Reader) mode in HDF5, follow these steps:

1. Ensure your file is on a POSIX-compliant filesystem and was created with the latest HDF5 format (`libver='latest'`).
2. Open the file for reading with `swmr=True` to enable SWMR mode.
3. Call `dset.refresh()` before each read operation to synchronize with the writer's changes.
4. Implement a polling loop or use inotify/custom IPC to detect new data as it is appended by the writer.
5. Read new data only, using slicing based on the dataset's current size.
6. Close the file when done.
1. **Structure upfront**: Create all groups and datasets before enabling SWMR mode.
2. **Flush regularly**: The writer must call `dset.flush()` after each write to make changes visible to readers.
3. **Refresh before reading**: Readers must call `dset.refresh()` to see the latest data.
4. **Use chunked datasets**: Required for extending data (set `maxshape=(None,)`).
5. **Set appropriate poll intervals**: Balance latency vs overhead (0.1-1.0 seconds typical).
6. **Use local filesystems only**: Network filesystems violate POSIX requirements.
7. **Handle crashes gracefully**: The file remains valid through the last flush.
8. **Test ordering**: Verify that readers open after writers enable SWMR mode.
9. **Document flush frequency**: Critical for readers to understand data freshness.
```


### WITH skill  (injected: `hdf5-clio-core-ingest`, rubric 2/4)

_rubric:_ ✗ hdf5:: routing (src prefix), ✓ context_bundle for ingest, ✓ CTE Tag.GetBlob for read, ✗ avoid context_retrieve for binary

```
The provided file context describes a skill named "hdf5-clio-core-ingest" which outlines best practices for ingesting HDF5 files into clio-core, a blob store managed by the CAE assimilator. The skill emphasizes that bundling HDF5 files into clio-core is not an optimization for read latency; in fact, it results in a significant per-object penalty (100-200x) compared to native h5py reads.

The key takeaways from this skill are:
1. Default to reading natively with h5py unless you need clio-core's shared-memory residency or multi-tier placement features.
2. Consolidate small datasets into larger ones before ingesting, as the cost scales linearly with dataset count (~11-20 ms per dataset for a full read and ~21-38 ms per dataset to enumerate).
3. Avoid bundling to make reads faster, as the intuition of paying ingest once for cheap reads forever does not hold true; clio-core's warm read is slower than a cold native read even for large contiguous data.
4. Retrieve subsets selectively by filtering at retrieve time rather than pulling the whole bundle, as retrieve cost scales with the number requested (K), not the file total (N).
5. Large contiguous datasets are the friendly case; high object counts are the hostile one due to clio-core's poor scaling with object/chunk count.
1. Consolidate small datasets into larger ones before ingesting, as the cost scales linearly with dataset count and results in a significant per-object penalty.
2. Avoid bundling HDF5 files into clio-core for read speed optimization, as it does not provide any latency benefits compared to native h5py reads. Instead, consider using clio-core for its shared-memory residency and multi-tier placement features if those are required.
3. Retrieve subsets selectively by filtering at retrieve time rather than pulling the whole bundle, as this reduces the cost associated with retrieving large numbers of datasets.
4. Use the CTE `Tag.GetBlob` method to read binary data back via the CTE data plane, ordering chunks by numeric index.
5. Be aware of common pitfalls such as using the opaque binary chunker when intending semantic per-dataset ingest and expecting attributes/hyperslab reads from the assimilator, which does not support these features.
```
