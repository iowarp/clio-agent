# End-to-end orchestrator capability demo — multi-agent system materially creates an HDF5 file

**What this shows:** the Tier-1 `ClioAgent` orchestrator (planner loop) taking an existing HDF5 file and producing a NEW, validated working HDF5 file through its tool-execution layer (`hdf5_rechunk_dataset` -> h5repack), gated by the write-path policy.


**What this is NOT:** skill-attribution. The file-creating tool layer is skill-independent, and there is no 'ingest into clio-core' MCP tool for the orchestrator to run. The `hdf5-clio-core-ingest` skill shapes *advisory* recommendations only — its value is shown separately in `SKILL_AB_DEMO.md`. The Tier-2 experts never mutate files (advisory by design); only the Tier-1 planner executes tools.


## Setup

- Model: **Groq `gpt-oss-120b`** (cloud). A capable model is required: local granite-8b and even gpt-oss-20b fumbled the tool arguments. This is a multi-step ReAct task, so model quality and the self-correcting retry loop both matter.

- Input: `/home/matthewlarson/Documents/iowarp_repos/translation/clio_core_bench/fixtures_200mb/A_contig.h5` (dataset `/field`, **contiguous**).

- Output: `/tmp/clio_e2e_rechunked.h5`.


## Result

- **File created end-to-end: True**

- Validation of output: valid=True, dataset `/field` shape=[163, 163, 163], **chunks=[50, 50, 50]** (was contiguous -> now chunked), dtype=float32.

- The chunk-layout change is impossible without h5repack actually executing, so the validated output is definitive proof of material effect through the agent.


## Transcript


### Turn 1  (routed to: `hdf5`)

**Prompt:**

> Call the hdf5_rechunk_dataset tool now with these exact arguments: filepath='/home/matthewlarson/Documents/iowarp_repos/translation/clio_core_bench/fixtures_200mb/A_contig.h5', object_path='/field', chunk_dims='50x50x50', output_filepath='/tmp/clio_e2e_rechunked.h5'. Do not set chunk_adjustment or make_contiguous. Execute it.


**Agent answer:**

```
The recent attempts to run **`hdf5_rechunk_dataset`** failed because the arguments did not satisfy the tool’s validation rules:

1. **`make_contiguous`** was omitted, but the schema expects a boolean value (even if the default is `false`).  
2. An empty string was supplied for **`chunk_adjustment`**, which is not a permitted value (`larger`, `smaller`, `half`, `double`).  
3. The final error reported an *ambiguous layout request*: the tool requires **exactly one** of the following layout parameters:
   * `chunk_dims`
   * `chunk_adjustment`
   * `make_contiguous=True`

Since you only want to specify a new chunk shape, you should call the tool **with `chunk_dims` alone** and **omit both `chunk_adjustment` and `make_contiguous`** (or explicitly set `make_contiguous=False`).

**Corrected call**

```json
{
  "filepath": "/home/matthewlarson/Documents/iowarp_repos/translation/clio_core_bench/fixtures_200mb/A_contig.h5",
  "object_path": "/field",
  "chunk_dims": "50x50x50",
  "output_filepath": "/tmp/clio_e2e_rechunked.h5"
}
```

Please retry the `hdf5_rechunk_dataset` tool with the arguments above. If the call succeeds, the dataset `/field` will be rewritten with 50 × 50 × 50 chunks at the specified output location. If any further error occurs, the returned message will indicate the exact field that needs adjustment.
```


### Turn 2  (routed to: `hdf5`)

**Prompt:**

> Use hdf5_rechunk_dataset with chunk_dims='50x50x50' (a string), object_path='/field', filepath='/home/matthewlarson/Documents/iowarp_repos/translation/clio_core_bench/fixtures_200mb/A_contig.h5', output_filepath='/tmp/clio_e2e_rechunked.h5'. Leave all other arguments unset and run it now.


**Agent answer:**

```
The hdf5_rechunk_dataset tool ran successfully. It rechunked the dataset '/field' (shape [163, 163, 163]) from its original chunk layout (None) to the requested chunk dimensions [50, 50, 50]. The rechunked file was written to /tmp/clio_e2e_rechunked.h5.
```


## Reading of the run

- **Turn 1** failed: the planner chose the right tool but supplied invalid arguments (empty `chunk_adjustment`, omitted `make_contiguous`); the tool's schema rejected the call. The planner then explained the validation errors.

- **Turn 2** succeeded: with the arguments corrected, `hdf5_rechunk_dataset` executed and wrote the validated output. The multi-step loop self-corrected.

- Takeaway: the system *can* materially produce HDF5 files end-to-end, but the path is multi-step and sensitive to model quality (needed gpt-oss-120b) and tool-arg precision.
