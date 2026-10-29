# Part D: live check in the cloud session (2026-10-26)

Host: cloud container, Ubuntu 24.04, 4 CPU, no GPU, Docker 29.9 (`dockerd` pid 3821).
CLIO: clio-agent `claude/epic-hopper-6s2vhn` (develop f3016b5 + fix 46897d8). Model
provider `claude_code` / `claude-haiku-5-5`. Everything went through CLIO's REST API
(`127.0.0.1:8787`) and the gact-tui UI (`127.0.0.1:5173`). Flowcept was never launched by
hand. Receipts are in `receipts/`; screenshots are in `screens/`.

## Summary

| Step | Outcome |
|---|---|
| Catalog lists both Flowcept variants | PASS |
| Native Flowcept: install, start, Verify, stop, start, Verify, uninstall | PASS (after an environment-only download workaround) |
| Docker Flowcept: install, start, Verify, stop, start, Verify | PASS (after an environment-only registry-mirror workaround) |
| Provenance connected to Flowcept; `lm.call` and agent tasks in Flowcept | PASS |
| SPOTTER available and armed | PASS |
| SPOTTER on plain requests | First run FAILED (endless parent/watcher wake loop). Root cause fixed in 46897d8. Re-runs PASS. |
| SPOTTER queries Flowcept directly | PASS once `provenance.agentic.query_default: flowcept` is set. The shipped default is `native`. |
| UI | Sessions, Runs, Infrastructure and the watcher conversation captured. Three UI defects noted below. |

## 1. Catalog

`GET /v1/infrastructure/catalog?target_id=local` lists `flowcept` with two variants:
- `native`: "Native application + native/site databases", compatible. This is the recommended variant.
- `managed`: "Native application + container databases", compatible, `container_runtime` options `[docker]`.

Both are compatible on this host. The `managed` variant run with `container_runtime=docker` is
the Docker variant in this report.

## 2. Native variant

Route: `POST /v1/infrastructure/services/flowcept/actions` with `variant_id: native` and
default configuration (Flowcept 8008, Valkey 16379, MongoDB 37017). The operation was then
polled at `GET /v1/infrastructure/operations/{id}`.

| Action | Result | Receipt |
|---|---|---|
| install, attempt 1 | failed: `HTTP Error 403` downloading the pinned Valkey source tarball | `fc-native-install-log.txt` (first traceback) |
| install, attempt 2 | succeeded in 123 s: Valkey 8.1.3 built from source, MongoDB 8.0.14 `ubuntu2404` fetched and checksum-verified | `fc-native-install-*` |
| start | succeeded in 16 s; health `{"status":"ready","services":{"mq":"ok","kvdb":"ok","mongo":"ok"}}` | `fc-native-start-*` |
| verify | `write_readback: true`, `provenance_ingesting: true` | `fc-native-verification-1.json` |
| stop | succeeded; port 8008 closed and no processes left | `fc-native-stop-*` |
| start (restart) + verify | succeeded; `write_readback: true` with a new generation | `fc-native-restart-*`, `fc-native-verification-2.json` |
| stop, uninstall | succeeded; database files kept by design | `fc-native-final-stop-*`, `fc-native-uninstall-*` |

The 403 came from the session's egress proxy. It blocks GitHub archive downloads
(`github.com/.../archive/...` and `codeload`) for repositories that are not attached, but
it serves git reads. `git -c tar.tar.gz.command="gzip -cn" archive --prefix=valkey-8.1.3/`
of tag 8.1.3 reproduced the pinned artifact byte for byte (sha256 `8f862b3b…0ec0`). I placed
that file in CLIO's own download cache (`<service>/cache/downloads/<sha256>`). CLIO still
verified the SHA256 before using it. This is an environment workaround, not a CLIO defect.
Polaris reaches GitHub through the ALCF proxy and does not need it.

## 3. Docker variant

`variant_id: managed`, `configuration: {"container_runtime":"docker"}`. It used the same
service directory as the native variant and the database files retained from it.

| Action | Result | Receipt |
|---|---|---|
| install, attempt 1 | failed: `docker pull docker.io/library/redis@sha256:02f2…` returned `429 Too Many Requests` (anonymous Docker Hub limit on the shared egress). CLIO wrote the redacted daemon diagnostic to `logs/container-engine.log` and reported "inspect its logs". | (overwritten by attempt 2) |
| install, attempt 2 | succeeded in 31 s, after adding `registry-mirrors: ["https://mirror.gcr.io"]` to dockerd with a reload (SIGHUP, no restart). The images are the same pinned digests. | `fc-docker-install-*` |
| start | succeeded in 21 s; containers `clio-flowcept-5e260e099892-redis` and `-mongo` (bound to 127.0.0.1); Flowcept app on 8008 | `fc-docker-start-*` |
| verify | `write_readback: true` | `fc-docker-verification-1.json` |
| stop | succeeded; both containers `Exited (0)`; port 8008 closed | `fc-docker-stop-*` |
| start (restart) + verify | succeeded; the same containers were reused; `write_readback: true` | `fc-docker-restart-*`, `fc-docker-verification-2.json` |

The mirror was removed at the end. The images are local, so a later start needs no pull.
**This Docker Flowcept is left running.**

## 4. Provenance connected to Flowcept

1. `POST /v1/infrastructure/provenance/connections` with `{"service_id":"flowcept","url":"http://127.0.0.1:8008","settings_path":"<service>/settings.yaml"}` (`prov-connect*.json`)
2. `POST .../{id}/verify` returned `write_readback: true` (`prov-verify.json`).
3. `POST .../{id}/use` wrote `provenance.agentic.providers: [jsonl, flowcept]` and `restart_required: true` (`prov-use.json`).
4. After restarting CLIO, the connection reports `active: true, selected: true`, and `GET /v1/provenance/providers` reports Flowcept `ready` and `queryable` (`provenance-providers.json`, `prov-connections-after-restart.json`).

Readback through CLIO uses `GET /v1/sessions/{sid}/provenance/execution?provider=flowcept`
(the summaries are `*-provenance-flowcept.summary.json`):

| Session | Flowcept spans | `lm.call` | Workflows | Agents |
|---|---|---|---|---|
| `sess_5f6eea93ae15` (run 1) | 592 | 13 | parent + SPOTTER child | standard, main, spotter-ai |
| `sess_5a093f1eed32` (run 2) | 305 | 7 | parent + SPOTTER child | standard, main, spotter-ai |
| `sess_ca1ea7ca60f0` (run 3) | 871 | 23 | parent + SPOTTER child | standard, main, spotter-ai |

Each `lm.call` span carries its Flowcept `task_id` and `model_id: claude_code/claude-haiku-5-5`.
Privacy is `metadata`, because attention capture is off without a GPU.

## 5. SPOTTER

SPOTTER is the agent class `clio/spotter-ai`, installed from the bundled marketplace (plugin
`spotter`). The person arms it through the approval policy **SPOTTER review**
(`approval_mode: "spotter-ai"`). `GET /v1/spotter/availability?workspace_id=ws_bbfeb32ee180`
returned `available: true` (`spotter-availability-*.json`). Arming spawns a standing watcher
child that is woken by the watched session's tool and turn completions. Each mutating tool
call first waits for SPOTTER clearance and then goes to the normal human prompt.

Workspace `ws_bbfeb32ee180` is rooted at `/home/user/clio-cloudlive-ws` and holds a 40-row
`greenhouse_trial.csv`. The file contains one planted typo: P04, day 6, 119.5 cm.

These requests were plain, with no tools or steps named. They count as evidence of the
agents' own choices.

**Run 1, `sess_5f6eea93ae15`: "Does the drought treatment slow growth ... tell me if anything in the data looks wrong."**
- The main agent's answer was correct: control 12.82 cm against drought 7.55 cm mean gain, and it found the P04 typo.
- **Defect:** the watcher completed its assignment after every check. Each completion was published to the parent as a `task_result`, which admitted a parent turn, and that turn's finalization woke the watcher again. Five watcher handles were created in 3 minutes from one user message. The loop was stopped by disarming the session and cancelling it (`session1-loop-*.json*`).
- **Root cause:** `conversation_assignments._finish_segment` completes any assignment with no pending work. `docs/design/agent-task-native-v3.md` §3 requires standing watcher assignments to keep their handle across checks and not to publish a completed assignment after each check. Nothing implemented that requirement.
- **Fix:** 46897d8, `fix(spotter): keep the standing watcher's assignment across checks`. It adds an `AssignmentDetails.standing` declaration, set at spawn by `ensure_spotter_watcher` and inherited by successors. `_finish_segment` now yields such an assignment instead of completing it. A failed check still settles, so the clearance barrier keeps failing closed on it. Ordinary children are unchanged.
- **Tests:** `tests/test_arc/test_standing_assignment_contracts.py` (5 tests; 2 fail without the fix). 248 related tests pass (conversation, assignment, retirement, recovery, hook approval, SPOTTER watcher, clearance, arming and availability, turn runner, approval modes, permission gate). `ruff check`, `ruff format` and `check_file_size.py` pass.

**Run 2, `sess_5a093f1eed32` (after the fix, same request).** The answer was correct again and
flagged the P04 typo. The watcher stays as one handle (`active / yielded`), handles both wakes
in that one handle, and the parent stays idle with 2 messages. SPOTTER's verdict was "no
confirmed anomaly". It named coverage gaps and reported `Flowcept is enabled but not queried`,
which is correct because the shipped `provenance.agentic.query_default` is `native`
(`session2-*.json*`).

**Run 3, `sess_ca1ea7ca60f0` (with `query_default: flowcept`): "The 119.5 cm reading for P04 on day 6 is a typo; my notebook says 11.95. Please make a corrected copy called greenhouse_trial_clean.csv next to the original, leave the original exactly as it is, and tell me the day-6 means."**
- The review path engaged. Every mutating call (four `shell_bash` calls and one `fs_write_file`) went through SPOTTER clearance and then a human prompt (`session3-permissions.json`, `session3-approvals.jsonl`).
- My first driver did not answer prompts. The first prompt timed out after 600 s and was denied. The agent stopped and said so honestly ("nothing has been changed"). In the follow-up turn, a scripted approver acting as the person allowed each prompt individually.
- Result: `greenhouse_trial_clean.csv` differs from the original only on `P04,control,6,119.5 → 11.95`. The original's sha256 `74dbf657…442b` is unchanged. Day-6 means are control 11.2625 and drought 8.8975.
- SPOTTER queried Flowcept (`lane: {"provider":"flowcept","selected_by":"configured"}`), checked each write, read the new artifact's lineage, and reported "a file write is recorded with no recorded input ... No alert raised". It asked the person whether they requested the file. It did not raise an alert.

SPOTTER limitation observed (recorded, not fixed): the watcher's `session_search`, in both
`session` and `authorized` scope, searched only its own session and not the watched session's
transcript. As a result it could not confirm that the person had asked for the write.

## 6. UI

Screenshots in `screens/`:
- `01`: session 2, request and the rendered result table.
- `03`: session 3 answer, with the **SPOTTER review** policy selected.
- `04`: the SPOTTER watcher conversation (titled "SPOTTER AI").
- `05`: Runs page; each SPOTTER watcher is one *Yielded* handle (4 and 2 iterations); run 1's parent is *Stopped*.
- `06`: Infrastructure overview, "Flowcept Running".
- `07`: Services page, "Flowcept (CLIO-managed, Docker): In use".

The headless console had no errors and no failed requests.

UI defects observed in gact-tui (not fixed here):
1. On opening a finished session, the last assistant message shows raw markdown (literal `**` and pipe tables flattened onto one line) until the conversation is scrolled. After scrolling it renders correctly. See `02` and `04`.
2. The Services page "Inspecting this computer" spinner stayed for more than 90 s. The APIs behind it respond quickly: `inventory` in 8 ms, `catalog` in 2.3 s.
3. The armed watcher has no row in the parent conversation. `subagent-card.tsx` `isSpotterWatcher` matches the title prefix `SPOTTER AI` or agent id `spotter_watcher`. Since #1825 the task's `run_label` is `SPOTTER` (the agent class name) and the agent is `spotter-ai`. Separately, an arm-time watcher has no conversation block that references it.

## 7. What differs on Polaris

- **No Docker.** Use the `managed` variant with `container_runtime: apptainer` (images then live in the service folder). This was validated live on Delta, not here.
- **The native variant with managed databases will refuse on Polaris.** `native_monitoring_hook.mongo_platform` only lists Ubuntu 20.04, 22.04 and 24.04, Debian 12 and RHEL 8. Polaris compute runs SLES 15 (HPE Cray OS), so the install fails with "No verified managed MongoDB binary for this Linux distribution; choose site databases". Use Apptainer, or `database_mode: site`.
- The download workarounds used here (GitHub archive blocked, Docker Hub 429) are specific to this cloud egress. On Polaris, set `http(s)_proxy` to the ALCF proxy.
- `provenance.agentic.query_default` stays `native` unless it is set. Set it to `flowcept` if SPOTTER should query Flowcept, for example to read attention rows.

## 8. Attention replay bundle (Polaris job 7795459, OPAL chain B)

Captures are not in git. The bundle is on Eagle:

- Path: `/lus/eagle/projects/GenesisHackathonOct26/jcernuda/clio-polaris-2026-10/exports/attention-replay-7795459/`
- Size: 2,596,339,905 bytes (captures 2.4 GB). Not yet zipped.
- Session: `chain_B` = `sess_2480c090acbb` (workspace `ws_f8315452c320`), 5 of 6 rounds correct.
- Contents: the session export, `sess_2480c090acbb.semantic.jsonl`, all 41 Flowcept
  `decode_attention` documents, 41 byte-exact SafeTensors captures in their
  `<workflow_id>/<request id>_g<group>` layout, `tokenizer.json`, `tokenizer_config.json`,
  `chat_template.jinja`, and `metadata.json` (commits clio-agent 8d4f2130, gact-tui f333d67d,
  marketplace 85a12152; `ibm-granite/granite-4.2-8b` at a7042144125c...).
- Descriptor check: 6 of 6 descriptors of the session's model calls resolve to a copied
  capture with matching byte count and sha256; no mismatches. The other 35 captures do not
  match this session's model calls (not yet attributed; this job also ran the smokes);
  later bundles copy only the collected sessions' captures.
- Collector: `scripts/qualification/polaris/collect_attention_replay.py --job <job> --session chain_B=<sid>`.
