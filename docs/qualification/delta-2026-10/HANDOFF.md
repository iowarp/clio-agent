# CLIO qualification campaign: handoff (Delta → Polaris), 2026-10-09

Read this first. It is the single entry point for the agent that continues this campaign on ALCF Polaris.

## 1. What this campaign is

Qualify CLIO (iowarp/clio-agent + UI iowarp/gact-tui + iowarp/clio-agent-marketplace) live on HPC compute nodes, through CLIO's own product interfaces (UI/REST), with fixes landed in the owning repos:

- **A. Local LLM servers:** vLLM, llama.cpp, Ollama (native and container). Covers discovery, capabilities, binding, real tool use, incremental streaming, and thinking kept separate from the answer.
- **B. Managed infrastructure:** model downloads, runtimes, Flowcept, CMF, web search, the attention connector, and APPL-CORE/SPOTTER blueprints. Each goes through install / configure / start / observe / stop / restart / reconnect / remove.
- **C. OPAL through CLIO:** vLLM with real attention capture, plus Flowcept, the attention views, and a SPOTTER investigation, followed by the OPAL live prompts.

The full original brief is `worker/CAMPAIGN.md` in the private evidence repo. The user's standing decisions are in `worker/DIRECTIVES.md` (items 1–31 plus 16a). They override the brief where they conflict. Read them.

## 2. Where everything is

| What | Where |
|---|---|
| Core fixes | **iowarp/clio-agent `qual/delta-fixes`** (draft PR #1675 → `develop`). Rebased onto the user's chosen base `develop@d4e8bd53` ("beta 5.3"). Do **not** rebase onto newer develop unless the user asks. The PR shows conflicts only because develop moved on. |
| UI fixes | **iowarp/gact-tui `qual/delta-ui`** (draft PR #568 → `main`), on gact-tui main `d32025b` |
| Marketplace fixes | **iowarp/clio-agent-marketplace `qual/delta-marketplace`** (draft PR #89 → `main`) |
| Submodule pins | `qual/delta-fixes` pins `external/gact-tui` → `qual/delta-ui` and `external/clio-agent-marketplace` → `qual/delta-marketplace` |
| Web search image | iowarp/clio-web-search **v0.3.2 released** (PR #6): `ghcr.io/iowarp/clio-web-search:0.3.2` (full) and `:0.3.2-slim` (default in CLIO). Digests are pinned in `web_search_service.py`. |
| Attention connector build fix | `spotter-ai-genesis/vllm-attn-connector`: pinned `95ab2ac` cannot build (SPDX license + License classifier). The fix is local commit `4c1a9fa` (not pushed; it's another org). CLIO accepts `CLIO_VLLM_ATTN_CONNECTOR=<PEP 508 ref>` to use a fixed build. **Recreate or push the fix first on Polaris** (one-line `setup.cfg`/`pyproject` classifier removal). |
| Evidence, failures, worker state, scripts | **private repo JaimeCernuda/clio-qual-delta-2026-10**: `evidence/` (receipts, traces, screenshots, tests, failures), `FAILURES-INDEX.md`, `worker/` (DIRECTIVES, STATE, HANDOFF, PLAN, CAMPAIGN, LOGIN-BRANCHES, MONITOR, scripts) |
| Delta originals | `/work/nvme/bekn/jcernuda/clio-qual-20261007/` (NCSA Delta, project bekn) |

## 3. What works (validated live on Delta, patched branch)

**A. Local LLM servers**
- **vLLM native (0.28 profile):**
  - full matrix: discovery and limits, tools, timed streaming, thinking separation (model-family parsers chosen automatically), knobs, cancel (stream really closed), kill/recovery, model switch, pinned sessions, compaction
  - default context = the model maximum
- **vLLM container (Apptainer).**
- **llama.cpp CUDA container (Apptainer),** including single-GGUF download and file-path serving.
- **Ollama CUDA container (Apptainer),** including on/off thinking and GPU-fitted context.

**B. Infrastructure**
- Model downloads: pinned revision, reuse, cancel, single file.
- Flowcept on Apptainer and CMF on Apptainer, each with a full lifecycle and Verify write/readback.
- clio-web-search on Apptainer (slim), including Valkey auth.
- Native SearXNG as the default `search.backend`, with the web MCP always declared.
- Install reuse preflight plus a live operation log (SSE).
- Context-sizing factory (Max / number / Fit-to-GPU), engine-neutral.
- Shared Hugging Face hub-cache layout listed as a model root (commits `73e143ff..d11e593b`; live check pending).

**C. Attention and SPOTTER**
- vLLM native-cuda-attention (profile `vllm-0.27.0-attention-1`) through CLIO, with managed Flowcept as the collector.
- `available=true` on plain turns, tool-using turns, and concurrent main+SPOTTER turns (`max_num_seqs=1`).
- Full chain: `lm.call` response_id ↔ vLLM `chatcmpl` ↔ Flowcept `decode_attention` task ↔ SafeTensors path/bytes/sha256.
- SPOTTER installed and armed on Flowcept+CMF; `spotter_query_tasks` returns the capture rows.
- A morning launcher (`worker/morning_session.sbatch`) brought the whole stack up on a fresh node in about 19 min. It was validated end to end through an SSH tunnel.

**Defects.** About 54 were found and fixed. The most important are F003/F005/F007/F009 (managed vLLM could not run at all on baseline beta.5), F010/F021/F026 (readiness and key identity), F037 (Ollama 404 under DSPy 3.4), F038 (cancel), and F046–F051 (attention chain alignment). See `FAILURES-INDEX.md`.

## 4. NOT done (prioritized)

1. **The OPAL run itself never executed.** The Delta GPU queue was saturated (Slurm job pending more than 10 h). It's ready to go:
   - Driver: `worker/opal_run.sh`, which sets up a tunnel, a workspace, a **read-only** source on the staged export, and an APPL-CORE session in SPOTTER approval mode pinned to the attention vLLM. It then runs `scripts/qualification/beta3_opal_run.py` over `docs/qualification/beta3/opal-live-inputs.json`.
   - Model chosen by the user: **Qwen/Qwen3.8-27B-FP8 @017b9c7af6b5** (or bf16 `Qwen/Qwen3.8-27B @1d4bf0f2ff60`). Its architecture is `Qwen3_5ForConditionalGeneration`, which is in the vLLM 0.27 registry.
   - The architecture is hybrid: 64 layers, of which 16 are full-attention (KV ≈ 64 KiB/token bf16 → about 16 GiB at 262k).
   - Parsers: `tool_call_parser=qwen3_coder`, `reasoning_parser=qwen3`. Request file: `evidence/traces/p2/opal-vllm-attn-qwen38-fp8.json`.
   - **Untested:** the connector's capture on hybrid layers, and CLIO's alignment for the VLM chat template. Test these first with one turn.
   - **Do not use Qwen3-4B for OPAL** (the user rejected it).
2. **OPAL data.**
   - The tabular core (85 files, 1.19 GB, sha256 + Drive ids in `evidence/opal-receipts/c49-staging-manifest.json`) was staged on Delta only. Re-stage it on Polaris.
   - The public Drive folder: <https://drive.google.com/drive/folders/1_bcYVgi8bj9umTup29sM2oZFEYKpg11W>.
   - **F059:** CLIO's no-sign-in public-folder mode (`gact/storage/linked.py::drive_folder`, gdrive-fsspec anon) needs a distributor **Google API key** in `CLIO_STORAGE_GOOGLE_API_KEY`. That key is NOT bundled anywhere; only the OAuth Desktop client id is bundled, in `oauth_clients.py`. Ask the user for the key and bundle it next to the client id. With it, CLIO can read the whole folder, including the ~60k image/mask files.
3. **The six-round A/B chain replay** needs `opal_chains.csv` from the user (`scripts/qualification/beta3_opal_inputs.py`).
4. **iowarp-core 2.3.1** (clio-core durability) is released. Apply directive 16a on `qual/delta-fixes`:
   - bump `iowarp-core~=2.3.1`, then `uv lock --upgrade-package iowarp-core` only;
   - retest F048/F023/F018/F019/F022, and close what 2.3.1 fixed.
5. **Live checks pending:**
   - HF-cache model root (register `<shared hf cache>/hub`, re-acquire models there; directive 31);
   - router live (2 vLLM instances behind LiteLLM Proxy);
   - attention UI in a real browser (full breakdown, readable labels, picker scrolling; settle the heat-profile control);
   - SPOTTER availability hint (it only shows once selected).
6. **Not started:**
   - llama.cpp native (Linux source-build fallback, F036) and Ollama native;
   - Windows native supervisor (F043);
   - F045 owner fix in clio-kit (web MCP crash before ready);
   - F055 connector owner fix (mixed prefill steps);
   - F056/F057 (per-host service roots pile up: this caused a project-wide **inode-quota** crisis on Delta);
   - SPOTTER lane reporting;
   - vLLM profile bump (pins are 0.27/0.28, latest is 0.31; F060).
7. **CI:** none has run on clio-agent #1675 (it conflicts with newer develop). Get CI by some means the user approves, e.g. a `campaign/**` branch PR, which ci.yml accepts. gact-tui #568 lint was fixed (`c29c547`); re-check its CI.
8. **Deliverables still owed:** `REPORT.md` (A/B/C status, baseline beta.5 vs patched), final provider/lifecycle matrices, `CLEANUP.md`.

## 5. Recommended first steps on Polaris

1. **Prepare the node.** Use PBS on Polaris (`qsub`), with 4×A100-40GB per node. There is no Docker/Podman; load Apptainer via modules. Outbound internet from compute nodes goes through the ALCF proxy; set `http_proxy`/`https_proxy` and verify. Put state, models and services on project storage (`/eagle` or `/grand`), not `$HOME`. **Check inode quotas first.**
2. **Clone the code.** Clone iowarp/clio-agent, check out `qual/delta-fixes`, run `git submodule update --init`, then `uv sync --frozen --extra dev --extra flowcept`. Build the UI from `external/gact-tui` with the CLIO brand profile (`brand.config.local.json` `{"profile":"clio","brandingRoot":"<clio-agent>/branding"}`).
3. **Redo the test suite once for portability** (the user asked for this). Run the focused files first, then broader, then record the Polaris differences.
4. **Bring up the stack through CLIO.** Order: Flowcept → (CMF) → vLLM-attention on Qwen3.8-27B. FP8 fits one A100-40GB with a fit-to-GPU context. bf16 needs tensor parallelism across 2 GPUs; attention-connector support for TP is untested. Run one attention turn and check the chain. Then run the OPAL broad prompts with `opal_run.sh` adapted to PBS/no-Slurm.
5. **Work method that worked.** Run a headless worker on the compute node in time-boxed chunks with `STATE.md`/`HANDOFF.md`/`DIRECTIVES.md`. Make the chunks **self-requeuing**, with a deterministic monitor. Do coding on the login node in parallel worktrees, and do running/testing only on compute nodes.

## 6. Hard rules from the user (abridged; see DIRECTIVES.md)

- **Fix blockers; don't just report them.** Fix in the owning repo, with tests.
- **Shared state:** never touch other users' files. Keep secrets out of logs, evidence and git.
- **Code organization:**
  - native and container variants for every managed service, on any runtime the host has;
  - cross-OS (Linux/macOS/Windows);
  - no home-grown router (LiteLLM Proxy or vLLM router only);
  - unsupported controls are hidden, not shown disabled.
- **Search works by default:** bundled SearXNG, with Chinese engines opt-in only.
- **Models live in the shared HF cache.**
- **Release steps:** don't merge, tag or release without asking. The user explicitly approved the clio-web-search 0.3.2 release.
