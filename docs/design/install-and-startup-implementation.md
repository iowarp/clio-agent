# Install, startup and loading on demand: implementation

## Context

**Why this started.** Flows fan out to hundreds of conversations. After months of use, a restart took
tens of minutes and the server held gigabytes, because Clio loads whole histories that nobody opened.
The target isn't a clean first start; it's the start after three months of flow-heavy use.

**Install.** Clio has three official ways in, and each produces a different Clio:
- the **download installers** (website);
- the **install script** (one line; without `--desktop` it is how servers, HPC nodes and SSH deployments
  get Clio);
- **from source** (through the script, or from a `git clone`).

They differ in Python version (3.13 versus 3.14), extras and install locations. There are four or more
copies of the "where is Clio installed" rules, and they have drifted. Several paths are broken:
- **The desktop launcher can't find a fresh script install.** It looks only in the legacy folders.
- **The lite desktop app installs from source** (`ref: develop`): it needs git, uv and Go, and that
  install can't be run again.
- **Repair does nothing.** The desktop sets `CLIO_FORCE`, but no installer reads it.
- **Bundled installs update to lite installers.** Both update feeds come from the lite build.
- **Upgrades and retries aren't safe.** Release mode deletes the venv before reinstalling, and downloads
  aren't checksum-verified.
- **No CI job runs the script, the launcher, uninstall, upgrade, repair or SSH deployment.**

**Startup.** On a normal start the UI shows "Unavailable / Connection unavailable" and never recovers on
its own. Shutdown has no deadline (#1401).

**The design** is [install-and-startup-plan.md](install-and-startup-plan.md).

**Decisions:**
- The installer program is **Rust**. The desktop's install code is already Rust, so the program reuses
  it instead of porting it.
- The installed command is **`clio-agent`**.
- Where the runtime comes from: **the download installers carry it, the script downloads it, the source
  path builds it**.
- The TUI is no longer installed.

**Rules for every PR:**
- correct before fast;
- no silent fallback (typed reasons);
- delete what is replaced in the same PR;
- no accretion (owner modules, size ratchets);
- plain user-facing copy with no "·";
- open PRs into develop; the maintainers merge.

## The runtime model (every track follows it)

The same model is in the design document; it is repeated here so this plan reads on its own.


**Durability.**
- clio-core is the record. Every write (messages, task events, mail, workflow state, session metadata)
  is committed to clio-core before it is acknowledged or acted on.
- Clio's memory is only a read cache, so Clio can die at any moment and lose nothing.
- Shutdown and a crash are the same event. Work that was running is recorded as interrupted and becomes
  an obligation on the next start.

**Four kinds of work, each with its own trigger and priority:**

| Kind | What | Triggered by | When |
|---|---|---|---|
| **Startup** | What makes every later request correct: clio-core connected, workspaces, the rail's first page of records (title, time, status flags), the marketplace and the default agent resolved, the provider checked | the process starting | before `ready`; nothing else runs first |
| **View** | What a person is looking at: rail pages; the opened conversation's latest page; then at once in the background its panels (tasks, child conversations, artifacts, workflow state) and its agent with that agent's MCP servers; older pages on scroll | a person's action | immediately on that action |
| **Obligations** | Work Clio owes whether or not anyone is looking: pending mail, interrupted turns, open tasks waiting on results, questions answered while it was down, scheduled tasks | the open-work records in clio-core | after `ready`, in the background, on their own |
| **Maintenance** | Cache pruning, the marketplace update check, garbage collection | timers | when idle |

**Rules:**
- **A lower row never delays a higher row.**
  - Startup never waits for obligations.
  - A click never waits for obligations or maintenance.
  - An obligation never waits for a click.
- **Obligations drain in bounded batches, oldest first** (concurrency is a setting).
  - Each loads only its own working set, runs, and releases it.
  - A three-month backlog of flow children can't flood memory or the model provider.
  - An obligation whose conversation is opened is either already running or moves up to view priority.
- **The cache keeps an item while a view or a running obligation uses it**, and releases it when
  neither does. Memory is bounded by the open views plus the running obligations, never by history.
- **Startup cost is flat in history.** No conversation contents and no task trees are read before
  `ready`.
- **Install follows the same split.**
  - Install puts every component on disk.
  - Migrations run at install or upgrade, never at boot.
  - An agent's MCP servers are prepared when a view or an obligation loads that agent (B3).

## Track R: implement the model

**What changes (locations from the memory search):**

- **R1. Startup does the startup row, and only that.**
  - `/v1/health` reports `ready` once those items are verified (B1).
  - Everything that today loads per-conversation state during boot moves to the obligations lane:
    - the pending-mail wake. `pending_sessions` runs three times today (`app.py:875`,
      `conversations.py:120`, and `notify(restored=True)` in `task_history_queries.py:497`), and each
      run loads whole task trees;
    - `restore_pending_mail_messages` (`session_store.py:67`), which loads whole message lanes;
    - `reconcile_open` (`task_index_writes.py:313`) and the recovery passes (`conversation_recovery.py`,
      `native_task_recovery.py`, `external_tasks.py:80`, `questions.py:170`), which load a tree for
      every open task;
    - `question_events.py:148`, which iterates every session's metadata;
    - `_reconcile_restart_interrupted_sessions` (`session_store.py:246`), which iterates every session.
- **R1b. The obligations lane.**
  - New owner module `gact/obligations.py`. After `ready`, it reads the open-work records once (mail,
    open tasks, interrupted turns, pending questions) and schedules them oldest first, with bounded
    concurrency.
  - Each obligation loads its working set through the task lane (R5) and the cache owner (R6), and
    releases it when done.
  - It uses the task layer's existing spawn and wake paths (`turn_runner.signal`, `Conversations`); it
    adds no new scheduler.
  - Opening a conversation that has a queued obligation moves it to the front.
- **R2. Migrations run once, at install or upgrade, not at boot.** These move into an upgrade step that
  the installer runs (`clio-agent migrate`), with a done marker:
  - `migrate_transcripts`, today run every boot over every session (`transcript_migration.py:26`);
  - `migrate_history` and `_full_replay` (`task_migration.py`, `task_index_build.py:52`);
  - the artifact registry fold (`artifacts/registry_boot.py:151`).

  Boot only checks the markers (one record read each). A migration still owed (an install
  that skipped the step, a runtime swapped in without it) runs once at that boot, before
  `ready`, as the boot step `migration.<name>` with the typed reason `migration_owed_at_boot`
  and the `store_migrations` health row; boot does not refuse, because the desktop and
  SSH-deployed agents start unattended and every migration writes its marker last.
  - Owner module `gact/store_migrations.py` (the registry: a new migration is one function
    and one `StoreMigration` entry); command `gact/store_migrate_command.py`.
  - Run by `install.sh` / `install.ps1` after installing or upgrading, and by
    `document_install` in the desktop's install-time prepare (not its per-start reuse check).
- **R3. The rail reads small records.**
  - `SessionStore` (`sessions.py:266`) keeps every session's metadata resident and rewrites the whole
    `sessions.json` file on each change. It moves to clio-core records with a recent-first index.
  - `GET /v1/sessions` is paged with a cursor.
  - The first page of the rail is part of startup; in gact-tui, more pages load as you scroll.
  - *Done (first step):* one row per session in `sessions.sqlite3` beside the legacy
    `sessions.json` (`gact/session_records.py`; the JSON is imported once, marker in the
    table). A change writes one row; rows load on first use into a bounded cache. The table
    is not yet clio-core: moving it is a backend swap behind `SessionRecords`.
  - *API:* `GET /v1/sessions` without `limit`/`cursor` is unchanged (every row). With
    `limit` (and `cursor` from the previous page) it returns one page newest interaction
    first, plus `next_cursor` and `total`; `kinds=conversation,branch` is the rail's filter.
- **R4. Opening reads one page first.**
  - `GET .../messages` (`routes/messages.py:283`) materializes the whole ledger and then pages it.
    Instead it reads the latest page from the message lane by range, and older pages on scroll.
  - The rest of the conversation loads in the background, after its first page.
  - In gact-tui, the conversation view requests the first page, then the rest.
  - *Done:* `GET .../messages?limit=N` reads the newest page from the lane by range
    (`gact/transcript_pages.py`), `before=<message id>` the page before it; `next_cursor`
    names the oldest message of a page when older ones remain. Without `limit` the whole
    ledger is returned as before.
- **R5. A task tree reads task events only.** `load_tree` (`task_tree_state.py:149`) reads each
  member's whole `_events` log, including `lm.call` records with full prompts. Task events get their own
  lane, migrated once by R2's upgrade step.
- **R6. One cache owner, read-only, bounded.**
  - The cache holds no state that isn't already committed to clio-core. Any write path that updates
    the cache first and persists later is changed to write through clio-core first.
  - `ResidentLedgerSet` (`resident_ledgers.py`) becomes the single residency owner. It counts ARC
    scopes and task trees as well as messages, and releases them on idle or over budget.
  - Load paths that bypass it are routed through it: `SegmentStore._scopes` (`arc/segments.py:182`,
    unbounded today), `TreeCache` (it trims on load, not only at reconcile), and the `arc/memory.py`
    `_inv_index`.
  - Search and recall (`session_recall_search.py:250`, `context_reference_evidence.py:143`) materialize
    every session per request. Instead they query stored records without loading conversations into
    the cache.
  - `sessions_with_scope` / `scan_names` (`arc/segments.py:823`), which list every blob, are replaced
    by prefix queries.
- **R7. Proof.**
  - New `scripts/create_reference_store.py` generates a three-month flow-heavy store: thousands of
    conversations, mostly flow children, long transcripts with `lm.call` records, open tasks, pending
    mail and interrupted turns.
  - The startup job measures restart time and server memory on that store and on one four times
    larger. Both must be flat: the difference within noise.
  - It also opens 50 conversations one after another, idles, and checks that memory returns to
    baseline.
  - **Backlog test:** a store with hundreds of pending mails and interrupted flow children. Time to
    `ready` is unchanged. The obligations drain on their own, with peak memory bounded by the
    concurrency setting. A conversation opened mid-drain shows its first page without waiting.
  - **Crash test:** kill Clio with `kill -9` at random points during flow-heavy work (mid-turn, mid-mail,
    mid-workflow), then restart and check that no message, task event, mail or workflow state was lost.
  - `scripts/profile_session_memory.py` is updated (its docstring describes the old eager load).

## Track B: startup and setup

- **B0. Boot timing.** New owner module `gact/boot_timing.py` records each startup step's start and end
  to the startup trace and to a `boot` block in `/v1/health`.
- **B1. Honest readiness, and a UI that waits.**
  - `/v1/health` reports `starting` with the pending steps, then `ready` (`routes/system.py`,
    `routes/health_boot.py`).
  - In gact-tui, `use-connection-availability.ts`, `connection-page.tsx` and `status.tsx` show "Clio is
    starting", keep checking, and open the workspace by themselves when it's ready. A real failure,
    after a bounded wait, names the cause and the fix.
  - Remove "·" from Clio's UI copy (108 in gact-tui, 31 in the agent) and rewrite the
    "Unavailable / Connection unavailable" copy.
  - The composer stays enabled while the agent is starting. A message is queued and runs when the agent
    is ready; the existing deferred inputs in `agent_initialization.mark_agent_ready` cover this.
- **B2. Shutdown in seconds, by the durability rule** (#1401).
  - Every write is already in clio-core, so stopping needs no drain. Shutdown stops accepting
    requests, cancels running turns and tasks (each recorded as interrupted, so it becomes an obligation
    on the next start), releases clio-core, and exits.
  - Out go the unbounded waits: `turn_runner.drain`, the `native.settle_shutdown` loops, and
    `release_runtime_after_drain` waiting on the boot attach.
  - Boot tasks are cancelled, and MCP executors close in parallel.
  - The daemon stop and the desktop app's force-kill share one budget.
  - The crash test (R7) proves that stopping this way loses nothing.
- **B3. Plugin MCP servers are prepared when an agent loads them.** This is the plan already reviewed:
  - `tools/mcp_prepare.py`, separate from start, plus the clio-kit `--prepare` / `--status` options (iowarp/clio-kit);
  - `uv run --project` servers get `UV_PROJECT_ENVIRONMENT` in the cache, so spotter stops writing
    `.venv` into the plugin;
  - a `provision` phase, with one spawn instead of two;
  - a turn doesn't wait for a server it may not use (live tool refresh, `wait_for_tools`);
  - preparing and failed states, and reconnect for plugin servers.

  It is triggered by R4: opening a conversation loads its agent.

  As built (`claude/b3-mcp-prepare`):
  - `tools/mcp_prepare.py` provisions a server before it starts (`uv sync` into
    `<cache>/mcp-project-envs/`, or `clio-kit mcp-server --prepare <name>` when the installed
    clio-kit offers it, else the typed `mcp_prepare_provision_unsupported_launcher`), and keeps
    each server's visible state: `preparing` (phase `provision`, `launch` or `connect`), `ready`
    or `failed` with a typed `mcp_prepare_*` reason.
  - `gact/mcp_readiness.py` starts a cold server once: the first start connects directly
    (`mcp_prepare_first_start_direct`) and lists over that connection.
  - `GET /v1/sessions/{sid}/messages` (newest page) starts the warm-up; a request made while
    the agent is still starting runs when it is ready. Boot lists stdio servers from the cache
    only (`mcp_namespace_not_prepared_at_boot`).
  - A turn waits at most `tools.mcp.turn_prepare_grace_s` (2 s); a server still preparing is
    offered through `wait_for_tools` and joins the agent's tools at its next step.
  - `GET /v1/mcp/servers?session_id=` rows carry `status` `preparing`/`failed`, `reason` and a
    `preparation` block; `POST /v1/sessions/{sid}/mcp/{namespace}/reconnect` restarts one.
  - Still for clio-kit: `mcp-server --prepare <name>` (build the locked environment and exit 0)
    and `mcp-server --status <name>`.
- **B4. Nothing reinstalled or rescanned at boot; maintenance runs in its own lane.**
  - The marketplace check compares the recorded source version, not a full-tree hash
    (`plugins/service.py`; the mismatch itself was fixed on develop in #1854).
  - Cache pruning moves out of boot into `clio-agent doctor --gc` and the end of install and upgrade
    (`tools/mcp_cache.py`, `server_boot.py`).
  - The desktop app no longer runs `document_install` on every launch (the runtime ships it prepared,
    A1).
  - Heavy imports load on first use (#702).

## Track A: install

### Layout and manifest

- **Data root:** as `paths.py` defines it today. **App root:** `<data root>/app`, holding:
  - `runtimes/<version>/`: a DMG or deb records the runtime in place in the app bundle and copies
    nothing;
  - `office/<version>/`;
  - `bin/clio-agent`. It is on PATH through `~/.local/bin/clio-agent` on Linux and macOS, and on Windows
    through `%LOCALAPPDATA%\clio-agent\bin` added to the user PATH.
- **`<data root>/install.json`** (versioned) holds:
  - the install root;
  - `current` and `previous` runtimes;
  - the mode (release / dev / source);
  - components, with version and sha;
  - the desktop app path;
  - steps, each with its state, typed reason and duration;
  - migrations done;
  - the last check;
  - the port, pidfile and logs.
- **One backend:** the manifest names the one backend shared by the desktop app and the command line.
  It replaces gact-tui #311's `runtime.json` lookup.
- **Readers:** the Rust crate, and the Python module `src/clio_agent/install_manifest.py` used by
  `paths.py`, `doctor` and deploy.

### A0. Measure today (reporting, no failures)

- New `.github/workflows/install-matrix.yml` covers ubuntu, macOS (arm64 and x86_64) and Windows. It runs
  today's paths:
  - script, fresh;
  - source;
  - a re-run;
  - an interrupted install, then a re-run;
  - start, stop and uninstall;
  - NSIS silent, deb, and the unsigned DMG (reusing `smoke_desktop_package.py` and
    `smoke_macos_bundle.py`);
  - an SSH deployment into a Docker `sshd` container.
- It also has the R7 and B0 startup jobs on the reference stores.
- Timings go out as a JSON artifact and a job summary.
- **Deliverable:** the baseline per OS and path, written into the design doc, which then sets the
  budgets.

### A1. One runtime artifact, one builder

- **New `install/build_runtime.py`**, run with the pinned uv. It replaces `build-gact-runtime.sh` and
  `.ps1` and keeps the union of what the two do. It builds:
  - CPython 3.14 and the locked `clio-agent[argonne,desktop,flowcept]`;
  - clio-kit and the pinned `bin/uv`;
  - the **prepared document runtime** under `<runtime>/documents/`, which the lookup in
    `document_runtime.py:479` already expects;
  - the web UI;
  - `runtime.json`;
  - a `.tar.zst` archive and its sha256, with packing a build option on every OS.

  Versions live in one file, `install/pins.json`.
- **New `install/build_office.py`:** repacks LibreOffice per OS (shas already in
  `document_stack/office.py`) as `clio-office-<os>-<arch>.tar.zst`.
- **Release CI** (`clio-bundles.yml`): a `runtime` job builds both for all six targets, including Intel
  Mac and Windows ARM. The desktop job consumes that artifact.
  `check_release_completeness.py` is updated to match.
- **The lite desktop variant is deleted**, since every installer carries the runtime. That leaves one
  update feed per platform, pointing at the bundled installer.
- **Deleted:**
  - `build-gact-runtime.sh` and `.ps1`, `pack-gact-runtime.ps1`;
  - the lite matrix;
  - `latest-lite.json` and the `--variant` logic in `gen_tauri_update_manifest.py`.

### A2. The Rust program `clio-agent` and the bootstraps

- **Where it lives.** A new crate `runtime-manager` in the gact-tui desktop workspace, with a library
  and a binary.
  - **The library is the desktop's existing install code, moved, not rewritten:** `runtime_pack.rs`,
    `runtime_unpack.rs`, `installer_dir_swap.rs`, `installer_runtime_stop.rs` and the process-tree parts
    of `supervisor_shutdown.rs`, with their tests.
  - **New in it:** the manifest, a resumable step runner, fetch with resume and sha256 (`ureq`, already
    a dependency), the check, the source build, the desktop install, and the CLI (`clap`).
  - **The binary is branded** from `branding/clio/brand.json` at build time, the same way
    `gen-brand-backend.mjs` works today, so gact-tui stays brand-neutral.
  - **Linux builds are static musl**, so old HPC glibc is never a problem.
- **Commands:**
  - `install`, with `--desktop`, `--source`, `--dev`, `--version` and `--from <dir|archive>`;
  - `repair`, `upgrade`, `uninstall [--purge]`;
  - `migrate`, `start`, `stop`, `restart`, `status [--json]`, `logs`, `doctor`;
  - `run --host --port --token`, for the desktop's spawn contract.

  Any other subcommand (`serve`, `plugin`, `sandbox`, …) is passed to the runtime's Python `clio-agent`.
- **Install steps, each recorded with a typed reason:**
  1. resolve the version and target;
  2. get the runtime: carried, downloaded or built;
  3. get Office, in parallel with step 2;
  4. unpack (parallel zstd);
  5. migrate a legacy venv install;
  6. `clio-agent migrate`, for track R2 store migrations;
  7. switch `current` atomically, keeping `previous`;
  8. put the launcher on PATH;
  9. with `--desktop`, download, verify and install the app; with `--source --desktop`, run the Tauri
     build against the runtime from step 2;
  10. the check: start, wait for `ready`, stop;
  11. prune old runtimes and caches.
- **Resume and upgrades:**
  - `install` again, or `repair`, resumes from the first step that isn't done;
  - `upgrade` is `install` with a newer version;
  - settings and tokens are never touched (#1777).
- **Bootstraps.** `install/install.sh` and `install/install.ps1` shrink to about 30 lines:
  1. detect the OS and CPU;
  2. download `clio-agent-<os>-<arch>` and its sha256 (`--dev` uses the pre-release);
  3. verify it;
  4. exec `clio-agent install "$@"`.

  Run from a checkout, they pass `--source` with the checkout path. No bash on Windows.
- **Python side:**
  - `install_manifest.py`;
  - `paths.py` reads the app root from it;
  - `runtime/mcp_launcher.py` drops the `uv tool install clio-kit` advice;
  - one clio-kit pin, in `pyproject.toml`.
- **Deleted:**
  - the bodies of `install.sh` and `.ps1`;
  - `install/clio`, `clio.ps1` and `clio.cmd`;
  - `uninstall.sh` and `.ps1`, `desktop.sh` and `.ps1`;
  - `tests/test_scripts/test_namespace_installer.py`, `test_installer_python.py` and
    `test_release_install_policy.py`, replaced by Rust tests;
  - the TUI download and build;
  - the Go sidecar launcher (`desktop/sidecar-launcher`, `fetch-sidecar.*`).

### A3. Installers, the desktop app and SSH deployment use it

- **The desktop app** links the library. In-process, it calls install when the manifest doesn't name
  its runtime, run when it starts the backend, and repair. It needs no helper executable modes and no
  sidecar.
  - **Deleted:**
    - `execution_install.rs`, `runtime_install_report.rs`;
    - `supervisor_installer.rs`, `supervisor_install_command.rs`, `supervisor_install_events.rs`,
      `supervisor_update_verify.rs`, `package_version.rs`;
    - the `--prepare-runtime` and `--stop-managed-runtime` modes in `main.rs` and `lib.rs`.
  - `brand.json`: release mode, no `ref: develop`.
  - The in-app update runs the signed bundled installer through the Tauri updater. `update_channel.rs` and
    `desktop-updater.ts` lose the lite/bundled split.
- **Windows NSIS:**
  - postinstall runs `clio-agent install --from "$INSTDIR\clio-runtime.tar.zst" --desktop-app "$INSTDIR"`;
  - preinstall runs `clio-agent stop`;
  - uninstall runs `clio-agent uninstall`.

  The provider and infrastructure pages stay. **The MSI is dropped**, unless A0 shows a need.
- **macOS DMG, deb and rpm:** the first launch records the carried runtime and runs the check, with the
  existing startup progress UI.
- **SSH deployment** (`clio_agent_deploy.py`, `drivers.py`, `remote_lifecycle.py`,
  `remote_install_guard.py`):
  - install is the bootstrap with `--version <controller's version>`;
  - every other step is `clio-agent <cmd> --json`;
  - claim uses `status --json` instead of ss/lsof/proc and command-line parsing.
  - **Deleted:** `_ROOT`, most of `_COMMON`, `_INSTALL` and `INSTALL_GUARD`.
  - **Fixed:** the step classifier mislabels `start` (`ssh_transport_steps.rs`).

### A4. Fast and enforced

The runtime and Office downloads run in parallel, and unpacking is parallel. Budgets from A0 are
enforced in the matrix. Proposed: release install 1 min (3 min at most), upgrade 1 min, and repair and
uninstall always complete.

## Order and PRs

| Step | Repos | Depends on |
|---|---|---|
| A0 matrix (report), B0 timers, R7 reference stores | clio-agent | nothing |
| R1 to R6 loading on demand | clio-agent, gact-tui (rail and conversation paging) | B0, R7 |
| B1, B2 readiness, UI waiting state and copy, shutdown | clio-agent, gact-tui | B0 |
| B3 MCP preparation | clio-kit, clio-agent, gact-tui | R4 |
| B4 nothing reinstalled at boot | clio-agent | B0 |
| A1 artifact and builder, lite deleted | clio-agent, gact-tui | A0 |
| A2 Rust crate, `clio-agent`, manifest, bootstraps | gact-tui, clio-agent | A1, R2 (migrate step) |
| A3 installers, desktop, SSH | gact-tui, clio-agent | A2 |
| Budgets enforced (A4, R7, B) | clio-agent | A3, R6 |
| One `INSTALL.md`, website install page, design doc, issues closed | clio-agent | all |

Track R comes first among the code changes, because it is the user-visible failure that started this.
Every PR carries its deletions, its tests and its matrix results.

**Expected size:** about 8,000 lines removed across the install code (scripts, launchers, Go sidecar,
Rust install helpers, deploy shell). The Rust crate is mostly moved code plus about 1,500 to 2,000 new
lines, the Python builder about 600, CI about 400. Track R changes are mostly in existing owner modules.

## Issues covered

- clio-agent:
  - #702, #1232 and #1217: B3, B4 and R; #1217 re-measured first;
  - #1401: B2;
  - #1777: A2;
  - #906: the A2 check;
  - #889 and #893: R6.
- gact-tui #311: the manifest.

## Still open

- The budgets, set from A0.
- The macOS signing owner and date.
- Whether releases keep publishing the TUI.

## Verification

- **Unit tests:**
  - Rust: manifest, step resume, fetch with resume and bad sha, unpack, stop, upgrade and rollback;
  - Python: boot reads no conversation, paged sessions and messages, the task lane, the residency
    owner's limits, readiness, shutdown deadlines, MCP prepare;
  - vitest: the rail and conversation paging, the waiting state and copy.
- **Local suite:** `uv run --frozen --no-sync python -B scripts/run_tests.py tests/ -m "not integration"`,
  ruff, `scripts/check_file_size.py`, and the noqa/silent-fallback guards.
- **`install-matrix.yml`:**
  - install: OS × path × scenario (fresh, upgrade, repair after an interrupted install, uninstall);
  - startup on the reference stores: time to `ready` and memory flat between 1× and 4×; the
    background resume pass is timed separately; memory back to baseline after opening 50 conversations
    and idling;
  - the `kill -9` crash test: nothing lost after restart.

  It reports until the budgets are set, then enforces.
- **In a cloud session:**
  - `bash scripts/cloud/stack.sh start` on a generated reference store;
  - `node scripts/cloud/ui_check.mjs`: a blank conversation at once, the rail fills, clicking one shows
    its first page and then the rest, no failure state, no "·";
  - the boot block in `/v1/health`;
  - server memory (RSS) before and after.

  Windows, macOS and the desktop app are verified through CI only.
