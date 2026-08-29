# case13 (HPC Cluster Operator) — grind handoff

Written 2026-08-27 by the Gate S / F5 science-gate driver, mid-campaign,
context-saturation handoff. Read this fully before touching anything —
every trap below cost real live-cluster time to discover. This worktree
(`D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate`) is
the ONLY place to run case13 live cells from. **Do not use the main tree**
(`D:\Libraries\Documents\projects\clio-agent`) — it belongs to a concurrent
Codex session (its branch, its uncommitted changes); a live-cell run there
already collided once (see trap 1).

## Status as of this handoff

- S1 (`s1_capability`): reaches a real turn, the operator reasons correctly
  and grounds what it can in real tool output (confirmed LAMMPS installed
  via a genuine `spack_find` call), but **cannot yet dispatch** — the
  curated `jarvis_*` tools (`jarvis_create_pipeline`, `jarvis_describe`,
  `jarvis_add_step`, `jarvis_edit_step`, `jarvis_run`,
  `jarvis_get_execution`) and `relay_fetch_artifact` never mount into the
  session's tool grant, even though `RemoteMcpFederation` discovery now
  succeeds. **This is dispatched to a dedicated clio-agent implementer as a
  fresh fix (tracked informally — ask the owner for the issue number if one
  was minted); do not re-diagnose it, check whether it landed first** (see
  "How to tell if the mount-gap fix landed" below).
- S2/S3/S4: not yet attempted live — blocked behind the same mount gap
  (S2/S3 need `jarvis_run`; S4 needs a discovery-tool call, likely
  `remote_jarvis_jarvis_get_execution`/an equivalent listing tool, TBD once
  tools are granted).
- Two real, durable fixes already landed this session (below) that a fresh
  driver does NOT need to rediscover.

## How to tell if the mount-gap fix landed

Grep a fresh boot's server log for `custom_agent_tool_unavailable`. Zero
hits (or hits for tools OTHER than the jarvis_*/relay_fetch_artifact six)
means it landed. `run_bare_driver.sh` (below) already does this grep for
you at boot and prints it plainly — read that line before running a cell.

---

## 1. The environment recipe that works

Every value below was hard-won; do not substitute a value from
`ENV.md`/`README.md` in this case dir or from a stale memory — ENV.md's
relay recipe is for a **retired** `D:/relay-p5local` + ssh-tunnel stack.
This recipe is for the LIVE zero-ssh v1.7.0 `ares-p5run2` deployment.

```bash
# 1. Deployment identity -- RE-SOURCE THIS FRESH EVERY SHELL/SCRIPT
#    INVOCATION. Never cache the session id or generation in a variable
#    that outlives one script run -- the owner's teardown+start cycles
#    rewrite session.env underneath you, and a stale generation is refused
#    (typed "identity refusal") by the relay. Every script in this handoff
#    sources this file at its own top for exactly this reason.
. "/d/Libraries/Documents/projects/clio-deployments/bin/deployment-env.sh" \
  "/d/Libraries/Documents/projects/clio-deployments/ares-p5run2"
# ^ exports CLIO_RELAY_API_TOKEN, CLIO_RELAY_FRP_TOKEN, CLIO_RELAY_STCP_SECRET,
#   CLIO_RELAY_REMOTE_TRANSPORT_MODE=brokered_tcp, CLIO_RELAY_FRPC_BIN,
#   CLIO_RELAY_OWNER_SESSION_ID / _SESSION_GENERATION_ID / _OWNER_SESSION_CLUSTER
#   / _OWNER_SESSION_API_PORT (all read live from
#   D:\Libraries\Documents\projects\clio-deployments\ares-p5run2\session.env,
#   which the owner rewrites on every session teardown/restart), plus
#   CLIO_LM_PROVIDER/CLIO_LM_MODEL, CLIO_MCP_CALL_TIMEOUT_S, CLIO_SANDBOX_ENABLED,
#   CLIO_MCP_CONNECT_MODE from deployment.env. $RELAY is set to
#   $STATE_ROOT/bin/clio-relay.exe but as a POSIX-mangled path in some
#   invocations -- always export the LITERAL Windows form yourself (below).

# 2. Relay transport -- the three values that actually matter for clio-agent:
export CLIO_RELAY_EXE="C:\\Users\\jaime\\AppData\\Local\\clio-deploy\\ares-p5run2\\bin\\clio-relay.exe"
export CLIO_RELAY_MCP_URL='http://127.0.0.1:18795/mcp'   # the desktop door, MCP over HTTP. STABLE -- do not restart it yourself; it's the owner's/coordinator's resource.
export CLIO_RELAY_HTTP_URL='http://127.0.0.1:50837'      # see "CLIO_RELAY_HTTP_URL semantics" below -- this value is CORRECT even though nothing may be listening there right now.
export CLIO_RELAY_CLUSTER='ares-p5run2'

# 3. Workspace / file policy
export CLIO_CASE13_WORKSPACE_ROOT='D:\Libraries\Documents\projects\clio-runs\case13-v170'
export CLIO_ALLOWED_ROOTS='D:\Libraries\Documents\projects\clio-runs;D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate;C:\Users\jaime;/tmp'

# 4. gact server fixture port (pytest-only knob -- irrelevant to the bare
#    driver, which uses CLIO_BARE_DRIVER_PORT, default 17986)
export CLIO_GACT_FIXTURE_PORT=17970
```

### `CLIO_RELAY_HTTP_URL` semantics — read this before "fixing" it again

`ENV.md`/`serve.sh` (both retired-stack docs) set this to
`http://127.0.0.1:${CLIO_RELAY_OWNER_SESSION_API_PORT:-8798}` — a literal
ssh-tunnel local-forward that does not exist under zero-ssh brokered
transport. The real zero-ssh mechanism is a **per-need, ephemeral** frpc
STCP visitor (clio-relay#285: each brokered CLI/session invocation spawns
its own visitor, whose rendered config — carrying the stcp secret — is
deleted from disk right after spawn; orphaned visitors self-clean on their
owner's exit). There is **no stable, independently-restartable local port**
for this anymore. I found one alive once (bind port `50837`, config at
`%TEMP%\clio-relay-frp-visitor-*\frpc-visitor.toml`) and hand-restarted it
from its leftover config as a one-time diagnostic; it died again within
seconds and its config directory was gone. **Do not repeat that** — it's
not a supported operation and burns time for nothing.

The value `http://127.0.0.1:50837` is still the RIGHT thing to export
because `resolve_relay_transport_config()`
(`src/clio_agent/tools/relay_factory.py`) only requires both `mcp_url` and
`http_url` to be **non-empty strings** to construct `JarvisJobs` — it never
probes reachability at config-resolution time. `JarvisJobs` itself
(`src/clio_agent/tools/jarvis_jobs.py`) never calls the relay HTTP client at
all — pipeline create/add_step/run/get_execution all ride MCP `tools/call`
over the door (`CLIO_RELAY_MCP_URL`, stable). Only `relay_fetch_artifact`
and SSE console/task-event streaming use the HTTP client
(`RelayTransportClient._require_http_client()` in `relay_transport.py`), and
those will typed-fail cleanly (connection refused) if actually invoked while
no visitor is alive — that's an honest, typed degradation, not a crash, and
not something to chase unless a scenario specifically needs
`relay_fetch_artifact` (S2's Darshan log, S3's frame images — see section 4).
If you need that tool to actually work, you'd need to establish a fresh
visitor for THIS specific need (not hand-restart a stale one) — ask the
owner/coordinator; I did not solve this.

### Session identity gotchas (live-fire, not hypothetical)

- The owned ares session has **lease-expired twice** during this campaign
  (`ttl_seconds: 1800`, i.e. 30 min of inactivity kills it). The owner runs
  a keep-warm watcher during active work, but **long gaps between cells
  (deep diagnosis, writing docs, waiting on another agent) let it expire**.
  Running cells **back-to-back** is what actually keeps it warm — the
  owner's own guidance. If you're about to have a long gap, say so.
- One owned session id (`ares-v170-20260826`) got **permanently bricked**
  after its second lease expiry (both teardown and `start --replace`
  refused — "remote coordinator cleanup report reference is not exact").
  It is preserved as a live repro for a lifecycle bug fix elsewhere; **do
  not touch that session id**. The replacement,
  `ares-v170b-20260827` / generation `546e3f019ca44b6f8a7ad57d6b1d35ca`
  (as of this writing — **re-read `session.env` live, do not trust this
  written value**), is the current one.
- The desktop door (`:18795`) has been killed unexpectedly at least once
  mid-campaign by an unrelated process-filter over-match on someone else's
  cleanup command, unrelated to anything I did. If a call to `:18795`
  suddenly fails, check with the owner before assuming it's your fault or
  retrying blindly — it may already be back on a new PID.
- A `remote-mcp refresh --cluster ares-p5run2 --name jarvis` /
  `--name spack` (run as durable relay jobs, no door restart needed) is
  what fixed the `relay_catalog_discovery_failed` issue (see trap list). If
  discovery degrades again (new door build, new session), that's the fix
  to reach for, not further clio-agent-side diagnosis.

---

## 2. The bare-script driver — why, where, how

**Why pytest must not drive live cells here (tracked as clio-agent#1258):**
`tests/test_real_cases/conftest.py`'s `gact_server` fixture spawns the gact
server via bare `uv run clio-agent-gact ...` as a pytest-owned subprocess.
On this box, that launch path crashed clio-core's CTE runtime daemon **6
out of 6 times** (stderr:
`D:\a\clio-core\clio-core\context-transport-primitives\src\event_manager_win.cc:125
ERROR ### AddEvent EventManager::AddEvent: WSAEventSelect ADD failed: 203`,
then at teardown:
`clio_agent.arc.runtime_crash: clio-core runtime daemon crashed:
pid=### exit=3221226505 (0xC0000409)`), which corrupts session state enough
that the very next HTTP call (`POST /v1/sessions/{id}/agent-blueprint`)
404s — before any relay call is ever made. The identical server + identical
SUT flow (`clio_sut.ClioAgent`), launched **outside** pytest (a bare python
script), showed **zero crashes across 7+ live attempts**. Ruled out as the
trigger, with hard evidence, in order: process-tree duplication (captured
1s-resolution live snapshots across a full failing pytest run — always
exactly one clean chain, one listener, never a second server), `CLIO_ARC_STORE=local`
forced (same crash — so it's not gated by ARC-store selection, something
else in clio-core's runtime bootstrap starts unconditionally), bare `uv run`
argv match, LM-bind timing, semantic-trace-backend match, full
relay+sandbox env match, workspace-path reuse, httpx fast-timing race. Root
cause NOT found — something specific to pytest's own process being present
correlates with the crash, mechanism unknown. Track as clio-agent#1258 if
no issue exists yet; this is a real, reproducible, box-specific (Windows)
clio-core stability defect, not a case13/harness defect.

**The driver, durably persisted in this worktree (not a scratchpad — will
survive to a fresh agent/session):**

- `benchmark/case13-hpc-cluster-operator/bare_driver.py` — drives ONE
  scenario through the real `clio_sut.ClioAgent` SUT (same class pytest
  uses), then **imports and runs the real matcher functions directly from
  `tests/test_real_cases/test_case13_cluster_operator.py`** (not a
  re-implementation — literally the same functions, so a pass here is the
  same verdict pytest would produce). Prints a `MATCHER VERDICTS` block and
  an `OVERALL: PASS`/`FAIL` line. Exit code 0 = all matchers passed.
- `benchmark/case13-hpc-cluster-operator/run_bare_driver.sh` — the wrapper:
  sources the env recipe (section 1), starts its own gact server matching
  conftest.py's fixture env exactly (`CLIO_DEBUG=med`, `CLIO_KIT_PATH`,
  `CLIO_SEMANTIC_TRACE_BACKEND=file` + `CLIO_SEMANTIC_TRACE_PATH`), greps
  the boot log for the crash signature AND the discovery result AND the
  toolset-inventory gaps (prints all three explicitly — read them before
  trusting the run), invokes `bare_driver.py`, tears the server down.

**Usage:**

```bash
cd /d/Libraries/Documents/projects/clio_develop_workspace/case13-gate
./benchmark/case13-hpc-cluster-operator/run_bare_driver.sh s1_capability
./benchmark/case13-hpc-cluster-operator/run_bare_driver.sh s2_instrumentation
./benchmark/case13-hpc-cluster-operator/run_bare_driver.sh s3_visualization
./benchmark/case13-hpc-cluster-operator/run_bare_driver.sh s4_honest_negative
```

On Windows, invoke via PowerShell if the Bash tool is unreliable (it was,
all session — a persistent `unexpected EOF while looking for matching ''`
from something in the shell snapshot/profile, never root-caused; git-bash
via PowerShell always worked):

```powershell
& "C:\Program Files\Git\bin\bash.exe" "D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate\benchmark\case13-hpc-cluster-operator\run_bare_driver.sh" s1_capability
```

A live cell can run long (LAMMPS/spack setup + relay dispatch + poll to
terminal). **Launch detached and poll a log file — do not hold a
foreground/`run_in_background` call and wait on tool-notification alone**;
notifications on this box were observed to arrive very late (once ~40+
minutes after actual completion) even though the process itself had already
finished cleanly. Reliable pattern (PowerShell):

```powershell
$logOut = "C:\path\to\some.out.log"; $logErr = "C:\path\to\some.err.log"
$p = Start-Process -FilePath "C:\Program Files\Git\bin\bash.exe" `
  -ArgumentList '"D:\...\run_bare_driver.sh" s1_capability' `
  -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
# then poll: while (Get-Process -Id $p.Id -ErrorAction SilentlyContinue) { Start-Sleep 5 }
# or just re-read $logOut periodically -- it's safe to read mid-run.
```

**Where results land:**
`D:\Libraries\Documents\projects\clio-runs\case13-v170\<scenario>_barepy\bare_run_result.json`
— the full `agent_test.Run` (as `.to_dict()`), every matcher's verdict, and
`overall_pass`. Durable, never auto-cleaned, safe to diff across runs. The
gact server's own log for each run: `C:\Users\jaime\AppData\Local\Temp\bare_driver_server.log`
(overwritten each invocation — copy it out if you want to keep a specific
run's server log).

---

## 3. Install state (durable — already done, do not redo)

- **Worktree**: `D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate`,
  a `git worktree add ... origin/develop --detach` off
  `D:\Libraries\Documents\projects\clio_develop_workspace\serve-17900`
  (never run git in the main tree — it's Codex's). Was at develop `5f8c1fda`
  when created; **check `git log -1` before assuming it's current** — the
  mount-gap fix implementer may have advanced it, or you may want to
  `git fetch && git merge origin/develop` yourself once their fix lands (ask
  the owner first — coordinate, don't just pull mid-cell).
- **Own venv**: `uv sync --extra dev --extra claude-code` (the
  `claude-code` extra is easy to forget and was missing for one whole grind
  cycle — `ModuleNotFoundError: No module named 'claude_agent_sdk'` at bind
  time if you do).
- **`agent-test` harness**: was entirely absent from this machine (private
  repo, `github.com/JaimeCernuda/agent-test`). Cloned to
  `C:\Users\jaime\agent-test`, installed editable
  (`uv pip install -e C:\Users\jaime\agent-test`) into THIS worktree's venv.
  **`uv sync` evicts this on every run** (it's untracked in
  `pyproject.toml`/`uv.lock` by design — a side-channel dev install) — if
  you ever run `uv sync` again in this worktree (e.g. after pulling a newer
  develop), **immediately reinstall**:
  `uv pip install -e C:\Users\jaime\agent-test`. Verify with
  `uv run --no-sync python -c "import agent_test, claude_agent_sdk"`.
  **Never touch the MAIN tree's venv** — it also has this editable install
  (added earlier, before the isolation was needed) and that's fine to leave
  alone, but don't install anything else into it.
- **`cluster-operator` blueprint**: was never installed anywhere on this
  machine before this session (confirmed: absent from
  `%LOCALAPPDATA%\clio-agent\agent-blueprints\`). Installed **globally**
  (scope=`global`, matching the `spotter-ai`/`phenotype` precedent — the
  test harness's own `run_spec` allow-list has no per-run install path, so
  the blueprint MUST be pre-installed at a scope that persists across the
  different workspace roots each scenario uses). Root:
  `C:\Users\jaime\AppData\Local\clio-agent\agent-blueprints\cluster-operator\`.
  **Includes the question-field fix** (see below) — this is not the
  builder's original pack content.
  To reinstall (e.g. after a further pack edit), bring up ANY gact server
  (bare driver's wrapper does this for you automatically each run) and:
  ```bash
  curl -X POST http://127.0.0.1:<port>/v1/agent-blueprints/sources \
    -d '{"source": "D:\\Libraries\\Documents\\projects\\clio_develop_workspace\\case13-gate\\external\\clio-agent-marketplace", "id": "case13-gate-marketplace"}'
  curl -X POST http://127.0.0.1:<port>/v1/agent-blueprints/install \
    -d '{"source": "D:\\...\\case13-gate\\external\\clio-agent-marketplace\\cluster-operator", "scope": "global", "blueprint_id": "cluster-operator"}'
  ```
  (Windows path backslashes must be doubled in the JSON body.) Verify via
  `GET /v1/agent-blueprints` — check the `install.checksum` changed.
- **The question-field fix** (durable, in the worktree's checked-out
  submodule, **not yet committed** — the owner said they'll make the
  durable marketplace commit once the rerun proved it, which it did):
  `external/clio-agent-marketplace/cluster-operator/experts/operator.md`,
  the `signature.inputs` block — was `request:`, is now `question:`. Root
  cause: clio-agent's runtime hardcodes the raw chat message under the key
  `"question"` (`src/clio_agent/gact/agents/builders.py:1543`,
  `kwargs = {"question": question}` — same convention in `reactv2.py:449`,
  `spawn_runtime.py:228`, `arc/live.py`, `messaging.py`, `cli.py`), and
  `reactv2.py:184`'s dict-comprehension
  (`{name: input_args[name] for name in self.signature.input_fields if name in
  input_args}`) silently drops any signature field whose name doesn't match
  a key already present — so a pack whose root expert's signature uses any
  other field name receives NO input at all, with no error. **If the owner
  hasn't committed this to the real marketplace repo yet, it lives ONLY in
  this worktree's submodule checkout + the globally-installed copy** — do
  not `git submodule update` this worktree without checking first, it would
  overwrite the fix.

---

## 4. Per-scenario expectations and how to check them

All four prompts are in `benchmark/case13-hpc-cluster-operator/prompt*.txt`;
full scenario intent in `scenarios.md`. The matchers in
`tests/test_real_cases/test_case13_cluster_operator.py` are the ground
truth — `bare_driver.py` runs them directly, so trust its `MATCHER
VERDICTS` block over any manual re-reading of the transcript. But READ the
transcript anyway (`run.output` in the result JSON, or the printed excerpt)
— a green matcher is not proof of a good answer; the grind doctrine is
"never trust a green matcher without reading the trace."

- **S1 `s1_capability`** (`prompt.txt`): short LAMMPS Lennard-Jones melt;
  report final temperature/energy/pressure. Setup-if-missing is part of the
  ask (LAMMPS is confirmed already installed on `ares-p5run2`:
  `20240829.1`, `gcc@11.4.0`, dag_hash `p5gjmq4rseitqanua7mdd2zdnag4v3u2` —
  a `spack_find` result from this session, so "already installed, do
  nothing" is a correct answer path, not a shortcut). Matchers beyond the
  always-on four: `task_envelope_present`, `durable_task_record_or_typed_degradation`,
  `door_confirmed_terminal_success`, `answer_numbers_grounded_in_artifact`
  (re-extracts every reported number from `run.output` and checks it
  appears verbatim in the bytes of a real output artifact file — a model
  reporting round/approximate numbers will legitimately fail this; tell it
  to quote exact values).
- **S2 `s2_instrumentation`** (`prompt_s2.txt`): profiles the SAME LAMMPS
  simulation's I/O with Darshan (the prompt says "that simulation you can
  run" — referring back to S1, so S2 must target LAMMPS, not IOR or any
  other workload, even though IOR happens to also be spack-installed on
  this cluster from an unrelated background build). Same matchers as S1
  plus the Darshan-specific expectation that reported byte counts trace to
  a real Darshan log artifact (still checked by
  `answer_numbers_grounded_in_artifact` generically — no separate Darshan
  matcher exists yet). **Also do the clio-relay#278 live-verification** the
  original brief asked for: through the door (MCP `tools/call`, bearer +
  `Mcp-Session-Id` headers), call `relay_list_artifacts` passing S2's run
  `execution_id` (not job id) and confirm the server resolves the owning
  job, then `relay_read_artifact` one artifact. I did not reach this step —
  no S2 run has completed yet. Note clio-kit#376 (jarvis MCP interceptor
  target-binding gap) was open as of this writing; if S2 needs Darshan
  wrapping via `jarvis_add_step` and hits that gap, it's a known,
  already-filed issue, not a new discovery.
- **S3 `s3_visualization`** (`prompt_s3.txt`): a composed pipeline
  (simulation stage + image-producing stage), ≥3 per-frame images returned
  as workspace artifacts with lineage. Matcher:
  `visualization_frames_with_lineage` (≥3 `.png`/`.jpg`/`.jpeg` artifact
  records, each with a non-empty `sha256`). ParaView (`paraview@5.13.1`,
  `+python+mpi~qt ^[virtuals=gl] osmesa`, headless/pvbatch-capable) is
  confirmed spack-installed on `ares-p5run2` from an earlier campaign
  background build — the render half of this scenario should not need a
  fresh spack install. This scenario will need `relay_fetch_artifact` to
  actually work (pulling frame images back locally) — see the
  `CLIO_RELAY_HTTP_URL` semantics note above; if the visitor tunnel is
  dead, this is the scenario most likely to hit that gap for real.
- **S4 `s4_honest_negative`** (`prompt_s4.txt`): "did any earlier run leave
  results on the cluster?" Against this deployment (no prior case13 runs),
  correct answer is a genuine "nothing found," matcher-verified against a
  REAL discovery-tool call's listing (`s4_answer_agrees_with_real_listing`
  — requires at least one tool call whose name matches list/history/search
  × job/task/artifact/run/pipeline; a run with zero discovery calls fails
  outright, "no request means no answer" is not honest here — it must
  actually check). Does NOT need `jarvis_run`/dispatch — only a listing
  tool, so this scenario might be reachable even before the mount-gap fix
  lands if a listing tool (e.g. `jarvis_get_execution` used descriptively,
  or a `remote_jarvis_jarvis_*` equivalent) is available. Worth trying
  first if time is short.

---

## 5. Every trap burned this session (so you don't re-burn them)

1. **Shared main tree collision.** Running a live cell from
   `D:\Libraries\Documents\projects\clio-agent` (not this worktree) failed
   because a concurrent Codex session was actively switching branches and
   merging into that exact directory mid-run, invalidating the venv's build
   state and colliding with unrelated `clio-agent.exe` server processes
   (`:8787`/`:8790`, not ours, do not touch) holding a Windows file lock on
   the same `.venv\Scripts\clio-agent.exe`. Fixed by moving to this
   isolated worktree. **Never run a live cell from the main tree.**
2. **`agent_test` pytest plugin entirely absent.** `--provider`/`--model`
   flags didn't exist; `pytest --help` showed nothing. It's a private repo
   (`github.com/JaimeCernuda/agent-test`) documented only in
   `.claude/skills/grind-clio-case/SKILL.md`'s prose ("Jaime's `~/agent-test`
   pytest plugin"), never actually present at `~/agent-test`. `gh` was
   already authenticated as the owner — cloned + installed editable. If
   this is missing again on a different machine, that's the fix.
3. **`CLIO_RELAY_HTTP_URL` stale ssh-tunnel recipe.** See section 1's full
   writeup. Do not restart a dead frpc visitor and expect it to stay up —
   it's designed to be ephemeral and self-cleaning (clio-relay#285); a
   hand-restart from a leftover config is a one-time diagnostic trick, not
   a repeatable fix.
4. **`cluster-operator` blueprint never installed.** 404 on
   `POST /v1/sessions/{id}/agent-blueprint` on a totally fresh worktree —
   don't assume "the pack exists in the marketplace submodule" means "it's
   installed"; check `GET /v1/agent-blueprints` for the id, or just always
   run the install call once per fresh install-state (idempotent, safe to
   repeat).
5. **`ss` does not exist on native Windows.**
   `conftest.py::_kill_port`'s `subprocess.run(["ss", "-ltnp"], ...)` raises
   `FileNotFoundError`, caught silently — the function is a **complete
   no-op on Windows**. It provides zero protection against a leftover
   server on the fixture port. Not exploited as a root cause this session,
   but worth knowing if a "port already in use" symptom ever shows up.
6. **CTE daemon crash under pytest specifically.** See section 2 in full.
   The single biggest time sink this session (~4 hours of isolated
   diagnosis). Bare-script driver is the accepted workaround; do not
   re-attempt pytest-driven live cells on this box until clio-agent#1258 is
   actually fixed and proven.
7. **`request` vs `question` signature field.** See section 3. A pack
   whose root expert declares a custom-named typed input field silently
   receives nothing — no error, just an empty-feeling turn where the model
   correctly notices its own input is missing. If you author or edit any
   OTHER pack's root-expert signature, name the field `question`.
8. **`relay_catalog_discovery_failed` / missing catalog-revision meta.**
   Fixed by the owner via `remote-mcp refresh --cluster ares-p5run2 --name
   jarvis` and `--name spack` (durable relay jobs over the held channel, no
   door restart). If you see `WARNING | relay first discovery
   reason=relay_catalog_discovery_failed federation=ABSENT` in a fresh boot
   log, that's the fix to ask for — not a clio-agent bug, a deployment
   registration staleness.
9. **`TOOLSET-INVENTORY custom_agent_tool_unavailable` for the curated
   `jarvis_*` six + `relay_fetch_artifact`.** The CURRENT open blocker (see
   "Status" above). Grep every boot log for this line before trusting a
   run — `run_bare_driver.sh` already does it for you and prints the
   result plainly.
10. **Session lease expiry (twice) + one permanently bricked session id.**
    See section 1's session-identity gotchas. Keep cells back-to-back; flag
    long gaps to the owner in advance.
11. **Door killed unexpectedly, once, by an unrelated process.** Not caused
    by anything in this workflow. If `:18795` calls suddenly fail, check
    with the owner before assuming fault.
12. **Task-notification unreliability for long background runs.** At least
    once, a background task's completion notification arrived roughly 40
    minutes after the process had actually finished (confirmed by reading
    the log directly — the result was already sitting there, complete,
    unread). **Always poll the log file directly for long-running cells;
    don't trust silence to mean "still running."**
13. **The Bash tool itself was broken all session**
    (`/usr/bin/bash: -c: line 77: unexpected EOF while looking for matching
    ''`, on every single invocation, never root-caused — possibly a
    corrupted shell snapshot/profile script). PowerShell invoking
    `git-bash.exe` directly (as shown throughout this doc) always worked;
    use that pattern, not the Bash tool, on this box. The `Monitor` tool
    routes through the same broken backend — also unusable here; use a
    PowerShell polling loop instead if you need live log tailing.
14. **Quoting hell between PowerShell and bash when embedding `$VAR`
    references or nested quotes in a single inline `-c` string.**
    PowerShell double-quoted strings interpolate `$name` even when
    preceded by a backslash (backslash is not an escape character in
    PowerShell the way it is in bash) — this silently blanks out variables
    you meant to pass through literally to bash. **Always write a `.sh`
    file with the Write tool and invoke it by path** (as this whole handoff
    does), never embed a multi-variable bash one-liner as a PowerShell
    `-ArgumentList`/`-c` string.
