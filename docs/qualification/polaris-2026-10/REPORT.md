# Morning report: Polaris readiness (2026-10-26)

Goal: [GOAL.md](GOAL.md). Polaris entry point: [HANDOFF.md](HANDOFF.md). Branch
`claude/epic-hopper-6s2vhn` in iowarp/clio-agent, iowarp/gact-tui, iowarp/clio-schemas
(unchanged) and iowarp/clio-agent-marketplace. clio-agent pins both submodules to the branch.

## A. Bogdan's attention semantics: done, verified against his own code

Spec: spotter-ai-genesis/experiments `bench/opal/analytics` @ `ebe17be`
(`attn_analytics.py`, `render_heatmap.py`).

| His rule | CLIO now |
|---|---|
| `score[j] = Σ_i 0.5^i · val_all_max[step_i][j]`, unnormalized, item's first step weighted most | Default profile `attn-analytics` (`gact/attention/profiles.py`). The old uniform-mean and decayed-max presets stay in the heat-profile editor; "SPOTTER" is the new preset. |
| One vector per answer item | `POST /v1/sessions/{sid}/messages/{mid}/attention/items`: items are explicit spans, or one selection split at a delimiter (default `;`). Each item is reduced on its own decode steps, never unioned. |
| Whole prompt, per line, max over the line's tokens, each column scaled to its own max, ramp ` .:-=+*#%@` | Same payload: `lines[].levels`. UI: **Prompt heat map** in the attention banner (one numbered column per item, section labels, "Only lines with heat", Download CSV). |
| CSV `chain_id … prompt_token_ids, scores` | `POST …/attention/items/export` writes his exact columns, so `render_heatmap.py` reads it as is. |
| Unretained positions score 0; connector `-1` slots dropped | `-1` (`NO_ENTRY`) slots are now dropped. Before, CLIO refused the whole capture as malformed. |

**Parity proof** (`tests/test_gact/test_attention/test_items.py`, on the real capture fixture from job
3237185): his `aggregate()` and `render()`, copied verbatim into
`tests/test_gact/test_attention/spotter_reference.py`, produce the same per-token scores (exact at the
CSV's 6 digits) and the same ramp cell on every line. One subtlety: line levels are computed from the
6-digit published scores, because his renderer reads the CSV, and saturated maxima sit exactly on a
rounding tie (`9 · 1.0 / 2.0 = 4.5`).

Screenshots of the heat map over the recorded capture:
`evidence/attention-ui/heat-4-heatmap-split-period.png`, `heat2-5-heatmap-all-lines.png`.

**Known difference:** if the tokenizer merges a separator into an item's token (`373;`), CLIO's item
includes that token, while his script drops it. Items are character spans in CLIO.

### Questions only Bogdan can answer

1. **Items in free prose.** CLIO answers are not `ANSWER: a; b`. Today an item is the person's
   selection, split at a delimiter they choose. Is that right, or should items come from structure
   (list items, table cells, sentences)?
2. **Multi-call answers.** An answer built over several model calls (tool steps between them): is each
   call's output its own set of items?
3. **Reasoning tokens.** Should `<think>` steps count? Today a selection inside the reasoning maps to
   the reasoning's own steps.
4. **CLIO's additions.** CLIO still shows a "Sources" breakdown by uniform mean mass plus a "spread
   thin" residual, and its drill-down ranks by the mean because `val_all_max` saturates at 1.0. Keep
   them as secondary, or drop them?
5. **Below the cut.** Should a score of 0 below the top-10% cut be shown as blank (his ramp) or marked
   "not captured"?

## B. OPAL driver: done

`python -m scripts.qualification.opal_chain_run`:

- prepares any chain of `opal_chains.csv` (the A/B injection pair stays specific to
  `exp32-chain-0020-hard`);
- runs it through a fresh SPOTTER session (`beta3_opal_run`);
- exports every round's `ANSWER:` items as his CSV (`attention-<cond>.csv`);
- scores the answers locally (`score-<cond>.json`). The correct answers never leave the operator
  directory.

No teacher forcing. Tests: `tests/test_scripts/test_opal_chain_run.py`.

## C. Polaris kit: written, statically checked, not run on a GPU

See [HANDOFF.md](HANDOFF.md). CLIO itself deploys Flowcept **natively** and vLLM
`native-cuda-attention`. **Stage 1 serves Granite 4.2-8B on one A100** (no tensor parallelism; chat
template and tokenizer byte-identical to the 30B's; about 100k tokens of context). Stage 2
(`MODEL_STAGE=2`) serves the 30B on two A100s once stage 1 passes. Each stage runs one attention smoke
turn before OPAL.

- Checks: `bash -n`, shellcheck, `py_compile`, `DRY_RUN=1` of every step, and kit.py's control flow
  against a stand-in REST server.
- Routes are verified from the code, with file:line in HANDOFF.

## D. Live check in the cloud session: passed, one SPOTTER defect fixed

Full write-up: [cloud-live/RESULTS.md](cloud-live/RESULTS.md); receipts and screenshots are next to it.
Everything ran through CLIO's REST API and UI, on `claude_code` (no GPU, so no attention capture).

- **Flowcept deployed by CLIO, both ways.**
  - Native: install, start, Verify (write/readback), stop, restart, Verify, uninstall.
  - Docker (`managed` + `container_runtime=docker`): install, start, Verify, stop, restart, Verify.
  - Two workarounds were for this cloud container only, not CLIO defects: its proxy blocks GitHub
    archive downloads (a git archive reproduced the pinned Valkey tarball byte for byte, and CLIO still
    checked its sha256), and Docker Hub rate-limited (429), handled with a temporary registry mirror.
- **Provenance.** The Flowcept connection was verified and selected; after the restart it was active.
  CLIO read back 7–23 `lm.call` per session from Flowcept, from both the main and the SPOTTER
  workflows.
- **SPOTTER AI on plain requests** (no tools named), over a 40-row CSV with a planted typo:
  - The analysis answer was correct and caught the typo.
  - A write request was cleared by SPOTTER before every write and then approved by a person; the
    original file stayed unchanged.
  - With `query_default: flowcept`, SPOTTER read its records from Flowcept and returned a verdict that
    named coverage gaps.
- **Defect found and fixed: an endless SPOTTER loop.** Every watcher check completed its assignment.
  The completion woke the parent, and the parent's turn woke the watcher again: one message made 5
  watcher runs in 3 minutes. Fix `46897d8`: the watcher is a standing assignment and its checks yield,
  as `agent-task-native-v3` §3 already required. Tests:
  `tests/test_arc/test_standing_assignment_contracts.py`. **Without this fix every OPAL SPOTTER session
  would loop.**

### Consequences for the kit (applied)

- **Native Flowcept now runs on SLES 15** (Polaris). CLIO refused SUSE before. It now pins MongoDB
  8.0.14's `suse15` build (checksum from MongoDB, rechecked on a fresh download), and a missing system
  library is reported by name. Proven through CLIO's API in an openSUSE Leap 15.6 container (SLES 15
  userspace, gcc 7.5): install, start, Verify (write/readback), stop, restart, Verify.
  Evidence: [suse15-native/](suse15-native/). Its one system requirement is OpenSSL 1.1
  (`libopenssl1_1`, SLES 15's standard OpenSSL); the kit's preflight checks it. Apptainer stays as the
  labelled fallback.
- **SPOTTER reads Flowcept by product behaviour, not a kit override.** Using a Flowcept connection now
  also makes it the provenance query source, and disconnecting returns queries to the native journal.
  Proven on the openSUSE CLIO: after "use" and a restart, `default_provider: flowcept`, with Flowcept
  ready and queryable.

### Follow-ups not done tonight

Creating suggested tasks timed out in this environment, so they are listed here instead.

1. **gact-tui: the armed SPOTTER watcher has no row in the parent conversation.** `isSpotterWatcher`
   matches `SPOTTER AI` / `spotter_watcher`, but since #1825 the label is `SPOTTER` and the agent is
   `spotter-ai`. The fix should be structural: the backend exposes a role on the subagent run and the UI
   reads it, rather than matching strings.
2. **gact-tui: a finished answer shows raw markdown until you scroll.**
3. **gact-tui: the Infrastructure page "Inspecting this computer" spinner never clears**, although its
   APIs answer.
4. **clio-agent: SPOTTER's `session_search` cannot read the session it watches**, so it cannot confirm
   that the person asked for a write.

## Test evidence (cloud container, 4 CPU)

- **clio-agent** `tests/test_gact tests/test_scripts tests/test_arc -m "not integration" -n 3`:
  **9077 passed, 2 failed, 46 skipped** (58 min). Neither failure comes from this branch:
  - `test_artifact_raster_query[nc]`: the container lacks a NetCDF backend (`netCDF4`/`scipy`
    extras are not installed).
  - `test_child_startup_contracts::test_cancelling_startup_settles_its_worker_before_terminal_publication`:
    a 10-second wait timed out under load. It passes alone and 3 times in a row together with the new
    standing-assignment tests.
- **An earlier run under heavier load** also tripped the 180-second limit on two load tests
  (`test_composer_fix_round` 1000 messages, `test_a2ui_load_bounds`).
- **Other checks:** route-count guard updated (405 → 407); `check_file_size.py` and
  `check_no_class_in_function.py` pass; ruff passes.
- **gact-tui** `pnpm test`: core 57 files and 308 tests passed; web 575 files and 4044 tests passed.
  `pnpm lint` and `pnpm typecheck` pass.

## E. What is pushed

All on `claude/epic-hopper-6s2vhn`:

- **clio-agent:** attention semantics, items and export; OPAL driver; Polaris kit and HANDOFF; QA
  replay fixes; the SPOTTER standing-watcher fix; the evidence. Submodules are pinned to the gact-tui
  branch and to marketplace `1b5280c`.
- **gact-tui:** Prompt heat map, SPOTTER profile preset, items API client.
- **clio-schemas and the marketplace:** unchanged.

Nothing is merged, tagged or released, and nothing was pushed to spotter-ai-genesis.

## Open risks for Polaris

1. **Stage 2 only: attention capture under tensor parallelism on CUDA has never run.** Stage 1 avoids
   it (one GPU). The connector pin `95ab2ac` gathers per-rank maxima and rank 0 writes the file; the
   stage-2 smoke turn checks that the capture metadata shows global head counts (32 query / 8 KV heads).
2. **Native Flowcept on HPE Cray OS itself** is untested (verified on openSUSE Leap 15.6). Preflight
   records `os_release`, `cc`, `make` and `libssl_1_1` before anything installs.
3. **Connector build fix.** The pinned connector does not build without a one-line fix. The kit builds a
   patched wheel locally. Nothing was pushed to spotter-ai-genesis.
4. **ALCF details not checkable from here:** queue names (`capacity`), `/local/scratch`, module names
   for Apptainer, and the driver version (preflight flags a driver older than 570).
