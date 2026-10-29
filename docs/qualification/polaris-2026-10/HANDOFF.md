# Polaris handoff (draft, 2026-10-26)

Single entry point for the agent that runs CLIO on an ALCF Polaris compute node. Read
[GOAL.md](GOAL.md) first. Work without asking anyone; when something breaks, follow the
failure table, fix the cause in the owning repository if you can, and report it.

## What you do

On one Polaris node (4 x A100-40GB), CLIO itself deploys Flowcept, then vLLM
`native-cuda-attention`, then serves its web UI. Flowcept runs **natively** only when the node can
run the prebuilt binaries: Valkey's needs glibc 2.34 or newer (SLES 15 SP6 yes, SP5 no), and MongoDB's
verified `suse15` build needs `libopenssl1_1`. CLIO never compiles anything; otherwise use
`FLOWCEPT_VARIANT=managed` (Apptainer databases). Preflight checks both and says which. See
[Managed infrastructure](https://clio.iowarp.ai/docs/managed-infrastructure/). Two model stages:

- **Stage 1 (default, `MODEL_STAGE=1`): IBM Granite 4.2-8B** (`ibm-granite/granite-4.2-8b` @
  `a7042144125c`, bf16, 17.6 GB) on **one** A100. No tensor parallelism, so the attention capture never
  splits across GPUs. Its chat template and tokenizer are byte-identical to the 30B's (the template
  CLIO's alignment is verified on), dense, 40 layers, 32 query / 8 KV heads, 128k context.
- **Stage 2 (only after stage 1 passes OPAL, `MODEL_STAGE=2`): Granite 4.2-30B** (`@9e668ce1c538`) on two
  A100s (TP=2). Capture under tensor parallelism has never run on CUDA; expect about 40-50k tokens of context.

You run ONE attention turn, and only if it passes you run OPAL:

1. Bogdan's chain `exp32-chain-0020-hard` round by round, condition A then B, each in a
   fresh APPL-CORE session with SPOTTER approval;
2. the broad SPOTTER investigation over the staged OPAL export (read-only source).

History is the model's own answers (no teacher forcing).

## The kit

Everything is in `scripts/qualification/polaris/` on branch `claude/epic-hopper-6s2vhn`. Each
step writes a JSON receipt to `$ROOT/evidence/job-<pbs id>/` (stage-up:
`$ROOT/evidence/stage/`, OPAL: `$ROOT/evidence/opal/`). Every step is idempotent: rerun it to
resume. `DRY_RUN=1` prints every install command and REST call instead of running it; `00_preflight.sh`'s read-only probes (nvidia-smi, df/lfs, curl to the proxy hosts) still run and write their facts.

| Script | Does |
|---|---|
| `polaris.env.template` | Settings. Copy to `~/.clio-polaris.env`; set `PBS_ACCOUNT`. |
| `00_preflight.sh` | GPUs and driver, cgroup `pids.max`, Eagle project space and **inodes**, proxy reach. |
| `01_stage_up.sh` | Pinned uv/Node/pnpm, clones, `uv sync --frozen --extra dev --extra flowcept`, CLIO-branded UI, connector wheel. |
| `02_services_up.sh` | CLIO serve, storage, model, Flowcept, vLLM attention, provenance (+ CLIO restart), bind. |
| `03_attention_smoke.sh` | One turn; requires `available: true` on the answer and on its per-item view. |
| `04_stage_opal_data.sh` | 139 OPAL files by Drive id, sha256-checked, then `chmod a-w`. |
| `05_opal_run.sh` | OPAL workspace, read-only source, sessions, chain A/B, broad run. |
| `tunnel.sh` | Prints the SSH command for the user's desktop (`$ROOT/run/ENDPOINT.txt`). |
| `teardown.sh` | Stops only owned services and processes, removes this job's node-local scratch, logs to `$ROOT/evidence/CLEANUP.log`. |
| `polaris_job.pbs`, `submit.sh` | Unattended batch run of all of the above, serving until the walltime margin. |
| `kit.py` | The REST helper the scripts call (stdlib only; runs on SLES python 3.6 too). |

## Run order

Paths: `ROOT=/eagle/$PBS_ACCOUNT/$USER/clio-polaris-2026-10`, `K=$ROOT/src/clio-agent/scripts/qualification/polaris`.

0. **Settings (login node).** On the first run the kit is not on Eagle yet: `git clone -b claude/epic-hopper-6s2vhn https://github.com/iowarp/clio-agent.git /tmp/k`,
   `cp /tmp/k/scripts/qualification/polaris/polaris.env.template ~/.clio-polaris.env` and set
   `PBS_ACCOUNT`. Check the queues with `qstat -Q`. Use `capacity` (1-4 nodes, long walltime) or
   `preemptable`; use `debug` (1 h or less) only for the smoke.
1. **Stage up (login node, direct internet, 15-30 min the first time, seconds afterwards).**
   First run: `bash /tmp/k/scripts/qualification/polaris/01_stage_up.sh` (it clones into `$ROOT/src`); later runs: `bash $K/01_stage_up.sh`. From here on always use `$K`: compute nodes cannot see a login node's `/tmp`. Verify: `evidence/stage/01-stage.json` is `ok`, the submodule pins
   match the branch tips (otherwise WARNING lines), `build/clio-web/index.html` exists, and
   `build/connector/CONNECTOR_SOURCE.txt` holds `file://.../vllm_attn_connector-0.1.0-py3-none-any.whl`.
2. **Get a node.** Interactive: `qsub -I -A $PBS_ACCOUNT -q capacity -l select=1:system=polaris -l filesystems=home:eagle -l walltime=04:00:00`.
   Unattended: `bash $K/submit.sh` (from `$K`, never `/tmp/k`: it passes its own directory to the job as `KIT_DIR`); this runs steps 3-8 and holds the stack until the walltime
   minus `TEARDOWN_MARGIN_S`. PBS gives no warning signal before the walltime kill.
3. **Preflight (1-3 min).** `bash $K/00_preflight.sh`. It stops below `INODE_MIN` (400k) free project
   inodes or `SPACE_MIN_GB` (150 GB). Read `00-preflight.json`, `inode-census.txt` and `gpus.csv`.
4. **Services (first node 40-90 min; a node that has them already: 10-15 min).** `bash $K/02_services_up.sh`.
   Expected receipts and times:
   - `02-serve`: CLIO boots in 2-5 min on Lustre and waits for "agent ready".
   - `04-model`: 0 min if the hub snapshot is complete. A CLIO download through the proxy: 8B (17.6 GB) 5-20 min, 30B (58.5 GB) 20-60 min.
   - `05-flowcept`: `native`, 1-3 min (prebuilt Valkey 8.1.3 and MongoDB 8.0.14 `suse15`, both
     checksum-verified and run once before install completes; a host they cannot run on is refused
     with the missing library named), or `managed` (Apptainer databases).
   - `06-vllm`: env install 10-20 min, then start 3-10 min.
   - `07-provenance`: verify, use, CLIO restart, `active: true`. "Use" also makes Flowcept CLIO's
     provenance query source, so SPOTTER reads the captured tasks; check `default_provider: flowcept` in
     `GET /v1/provenance/providers`.
   - `08-bind`: reachable, lists the model, `credential_ref` set.

   Check `vllm-context-sizing.json` and the effective `max_model_len` in `06-vllm.json`.
5. **Attention smoke (1-3 min).** `bash $K/03_attention_smoke.sh`. Pass:
   - `09-attention-smoke.json` has `status: ok`, `attention_available: true` and `items_available: true` (profile `attn-analytics`);
   - `thinking_separated: true`;
   - `recent_capture_files[].metadata` shows global head counts (32 query / 8 KV heads; at stage 2 also TP=2), not one rank's slice.

   **Do not start OPAL if this fails.**
6. **OPAL data (2-10 min).** `bash $K/04_stage_opal_data.sh`. Pass: `11-opal-data.json` is `ok` with 139 files and 1,237,266,797 bytes.
7. **OPAL (chain: 6 rounds x 2 conditions, about 0.5-2 h; broad: up to 3 x 40 min).** `bash $K/05_opal_run.sh`.
   - Outputs: `$ROOT/operator/chain-exp32-chain-0020-hard-{A,B}/attention-<cond>.csv` and `score-<cond>.json`, plus `evidence/opal/broad.json`.
   - Sessions are in `evidence/opal/opal-sessions.json`.
   - The operator directory holds correct answers. Never put it in a workspace, a prompt or git.
8. **Hand the UI to the user.** `bash $K/tunnel.sh` and report the printed command (UNIX-socket forward through
   the login node). The attention view must follow Bogdan's semantics (GOAL A). Take screenshots
   through the tunnel if you can.
9. **Teardown.** `bash $K/teardown.sh` (the batch job does this itself). Verify `14-teardown.json` and `CLEANUP.log`.

## Zoom demo (interactive job)

The user shares their own screen; only their laptop connects to CLIO.

1. In an interactive job (`qsub -I ...`), run steps 3-7 directly, not `submit.sh`. Leave CLIO, Flowcept
   and vLLM running afterwards: do **not** run `teardown.sh` until the user says the demo is over, or
   when less than 15 minutes of walltime remain (`qstat -f $PBS_JOBID`).
2. Before handing over, have a finished OPAL session with captured attention: answer turns marked
   available in `GET /v1/sessions/{sid}/attention/availability`. Report its session id and one good
   answer to select (an `ANSWER:` line with two items, e.g. round 3 or 5 of the chain).
3. Run `bash $K/tunnel.sh` and give the user the printed command.
4. The user runs it on their laptop, keeps that terminal open, and opens `http://127.0.0.1:8100` in a
   browser: CLIO serves the web UI built from this branch, with the Prompt heat map. A desktop app works
   too if it was built from gact-tui `claude/epic-hopper-6s2vhn`: add an agent service at
   `http://127.0.0.1:8100`.



| Symptom | Do |
|---|---|
| Preflight: low inodes/space | Inspect `inode-census.txt`. Uninstall old hosts' services through CLIO (UI or `action: uninstall`), prune `cache/`. Never touch other users' files. `FORCE=1` only after freeing space. |
| Driver < 570 / "CUDA driver version is insufficient" in the vLLM op log | Set `CUDA_COMPAT_DIR` to a cuda-compat lib dir (A100 supports forward compatibility; look under `/soft/compilers/cudatoolkit*/*/compat`) and rerun `02`. Otherwise report it: attention is native-only. |
| Op failed (`failed-*.json`, `ops/*-log.json`) | Read the CLIO op log first. Native Flowcept names what it lacks, e.g. "Valkey's prebuilt Linux build does not run on this host: ... GLIBC_2.34 not found" or "MongoDB's verified suse15 build ...: libssl.so.1.1" (SUSE package `libopenssl1_1`). CLIO does not compile anything: if the node cannot run them, use `FLOWCEPT_VARIANT=managed` (Apptainer via `POLARIS_MODULES`). |
| `02-serve` degraded / boot > 10 min | Lustre-slow boot or the bundled SearXNG install holding the target lock (Aurora A08). Wait. If it repeats, set `CLIO_SEARCH_AUTO_INSTALL=0` and record it as a deviation. |
| vLLM OOM / KV too small | Lower `VLLM_MAX_MODEL_LEN` and record the change. The 8B on one A100 leaves about 100k tokens of KV; the 30B at TP=2 about 40-50k. |
| Smoke `available: false` | Record `attention_reason`. `attention_capture_partial`: keep `max_num_seqs=1`. Missing capture file: inspect `06-vllm.captures`, the Flowcept `decode_attention` task and the connector log in the vLLM op log. Check whether one or both ranks wrote it. A head count of 16/4 means a per-rank slice: a connector TP defect. Fix it in the connector (another org: commit locally, do not push). |
| SPOTTER unavailable at OPAL setup | Read the reason in `failed-opal-setup.json`. A cold MCP env sync timed out on Aurora (5m44s): open a SPOTTER session once to let the warmup finish, then rerun `05`. |
| Driver refuses resume ("receipt belongs to different ... CLIO") | The endpoint or session changed (keep `CLIO_PORT=8100`). Start fresh: delete that kind from `opal-sessions.json` and use a new `--out`/receipt. Keep the old evidence. |
| Old session errors after a CLIO restart (F048) | Start a new session; note it. |
| Turn waits for permission/user | The drivers never approve. Inspect it in the UI, record it, do not approve on the user's behalf. |

## Known risks

- **Stage 2 only: TP capture is unproven on CUDA.** The connector pin `95ab2ac` gathers per-rank maxima
  and rank 0 writes the file, with the head count taken from `VllmConfig`. Aurora's XPU TP=2 run was
  `available: true`. Connector `main` (`e857fd0`) lacks these fixes: never use it.
- **Context:** stage 1 (8B, one A100) leaves about 100k tokens of KV (160 KiB/token). At stage 2 each
  A100 holds about 27 GiB of 30B weights, leaving about 40-50k tokens (256 KiB/token); SPOTTER's watcher
  prompt exceeded 32k on Aurora.
- **Native Flowcept on SLES:** the prebuilt Valkey runs on openSUSE Leap 15.6 (glibc 2.38) and not on
  15.5 (glibc 2.31); not yet tried on HPE Cray OS itself. Preflight records `os_release`, `glibc` and
  `libssl_1_1` and stops with the fix when native cannot run.
- **Chat-template alignment:** Granite 4.2 uses `qwen3_coder` tool calls and the `nemotron_v3` reasoning
  parser, chosen automatically by CLIO, and a pre-opened `<think>`. Check that the thinking selections
  and answer heat line up in the UI.
- **Inode quota:** about 140k inodes per new host for the service envs (F056/F057). State, envs, model and
  uv cache sit on Eagle; vLLM/Triton/compile caches and container temp go to node-local `/local/scratch`.
- **Lustre locks:** slow first boot, SearXNG extraction on Lustre (A08).
- **pids limit:** Aurora's job cgroup had `pids.max=8192`. No test suite runs here.
- **Proxy:** uv, HF, GitHub and Drive all go through `proxy.alcf.anl.gov:3128`. Loopback is excluded via `no_proxy`.

## Hard rules (DIRECTIVES, abridged)

- **SPOTTER reads Flowcept** because using the Flowcept connection makes it the query source (product
  behaviour on this branch, no override). Never set `CLIO_PROVENANCE_QUERY_DEFAULT` by hand.
- **Deployment goes through CLIO's product interfaces (REST/UI).** Hand-rolled launches are labelled
  fallbacks only. Every service has native and container variants on the host's runtime (native first; Apptainer is the container runtime here).
- **Fix blockers in the owning repo, with tests.** Do not just report them. Do not merge, tag, release or
  push to `spotter-ai-genesis/*`.
- **Never touch other users' files.** Keep secrets (API keys, tokens) out of logs, evidence and git.
  Correct answers stay in `$ROOT/operator`.
- **No home-grown router.** Hide unsupported controls rather than showing them disabled. Search works by
  default (bundled SearXNG).
- **Own your processes and data.** Record them, stop only what you started, and report forced stops honestly.

## Report back

- Which steps passed, with receipt paths.
- Timings and the effective context length.
- Smoke details: `response_id`, `capture_sha256`, capture metadata (TP, heads), and the items view.
- Chain A/B scores and the CSV paths (not the answers), plus the broad-run status.
- SPOTTER availability and alerts.
- Deviations (fallbacks, env overrides), new defects with fixes and commits, the cleanup record, and the tunnel command.

## REST routes the kit relies on (develop @ f3016b5 plus the branch)

All paths are under `src/clio_agent/gact/`. The attention line numbers move while that code changes.

| Route | File:line |
|---|---|
| `GET /v1/health` | routes/system.py:341 |
| `GET /v1/infrastructure/catalog` | routes/infrastructure.py:158 |
| `POST /v1/infrastructure/services/{id}/actions` | routes/infrastructure.py:168 |
| `POST /v1/infrastructure/services/{id}/context-sizing` | routes/infrastructure.py:208 |
| `GET /v1/infrastructure/operations/{id}` | routes/infrastructure.py:223 |
| `GET /v1/infrastructure/operations/{id}/log` | routes/infrastructure_operations.py:81 |
| `GET /v1/infrastructure/inventory` | routes/infrastructure.py:82 |
| `GET`/`PUT /v1/infrastructure/targets/{t}/storage` | routes/infrastructure_storage.py:55, :64 |
| `GET`/`POST /v1/infrastructure/targets/{t}/models` | routes/infrastructure_models.py:180, :227 |
| `GET`/`POST /v1/infrastructure/provenance/connections` | routes/infrastructure_provenance.py:92, :104 |
| `POST .../connections/{id}/verify`, `.../use` | routes/infrastructure_provenance.py:129, :143 |
| `GET /v1/provenance/providers` | routes/provenance.py:108 |
| `GET`/`POST /v1/providers/servers` | routes/local_servers.py:174, :182 |
| `POST /v1/providers/servers/{id}/check` | routes/local_servers.py:236 |
| `GET /v1/providers/{id}/models` | routes/provider_catalog_routes.py:123 |
| `GET`/`POST /v1/workspaces` | routes/workspaces.py:269, :282 |
| `GET`/`POST /v1/workspaces/{w}/sources` | routes/connected_storage.py:288, :314 |
| `POST /v1/workspaces/{w}/sources/{s}/link` | routes/connected_storage.py:591 |
| `GET /v1/agents` (replaces `/v1/agent-blueprints`) | routes/plugins.py:274 |
| `POST /v1/sessions` (`agent: {id: <agent ref>}`, `approval_mode: spotter-ai`) | routes/session_creation.py:25 |
| `GET /v1/sessions/{sid}` | routes/sessions.py:162 |
| `GET`/`POST /v1/sessions/{sid}/messages` | routes/messages.py:294, :254 |
| `GET /v1/sessions/{sid}/attention/availability` | attention/routes.py:212 |
| `POST /v1/sessions/{sid}/messages/{mid}/attention` | attention/routes.py:226 |
| `POST /v1/sessions/{sid}/messages/{mid}/attention/items` (`/export` is used by the chain driver) | attention/routes.py:271, :281 |
| `GET /v1/spotter/availability` | spotter_availability.py:193 |
