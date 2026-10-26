# Installation and startup: plan

Status: plan, 2026-10-26. Findings this builds on: [install-and-startup-paths.md](install-and-startup-paths.md)
(install paths, Windows failures) and the startup measurements in this document.

## Goal

A well-architected installation and setup experience that is **correct and fast on every OS, for every
user**: Linux, macOS and Windows; servers, HPC nodes, laptops and desktops; people who install a release,
build from source, use an installer, or deploy Clio to a remote host over SSH.

Rules, in priority order:

1. **Correct first.** Installing, upgrading and starting Clio never fail on a supported setup. When
   something does go wrong, the user sees the cause and the fix, and a rerun repairs or resumes.
2. **Then fast.** Every path gets the same optimizations, because every path runs the same code.
3. **One implementation.** One installer, one runtime artifact, one record of what is installed.
   OS differences live in small, named branches of that one implementation, never in parallel copies.
4. **No silent fallback.** Every step that can fail reports a typed reason, in the installer output and
   in the app. A "warn and continue" step that leaves something missing is a bug.
5. **Design for scale.** Cost must not grow with the number of conversations, plugins or MCP servers.
   Judge designs at 15 MCP servers and thousands of conversations, not at 3 and 10.
6. **Measured, not assumed.** Every claim about speed or reliability comes from the CI matrix below,
   on all three OSes.
7. **Delete what is replaced, in the same change.** We never again keep two ways of doing one thing.

## How a person chooses

| I want to... | I use | |
|---|---|---|
| Install Clio from the website | **The installer**: click Download | The desktop app with Clio in it |
| Install Clio from a terminal | **The install script**: one line | Short enough for a business card. Without `--desktop` it installs just Clio, which is how a server, an HPC node or an SSH deployment from the desktop app is set up. With `--desktop` it adds the desktop app |
| Build Clio myself | **From source**, through the same script | For technical users and contributors. `--source` clones and builds for you. The docs also show how to do it from your own `git clone` |

From your own checkout, the source build is the same installer, run from the checkout instead of
cloning one:

```
git clone --recurse-submodules https://github.com/iowarp/clio-agent
cd clio-agent
./install/install.sh --source              # Clio, built from this checkout
./install/install.sh --source --desktop    # also builds the desktop app locally
```

(`install\install.ps1 --source` on Windows.) The script detects it is running inside a checkout and
builds from it, so a contributor's edits are what gets installed.

The website shows the installer first and the script second; the source build, both ways, is in the docs. While
the macOS installers are unsigned, the website's macOS download points to the script with `--desktop`
and to the source build instead.

## First impression: a fast, complete install, then setup in the background

A person judges Clio in its first minutes. Install and setup are different jobs:

| Stage | Synchronous? | Does | Budget |
|---|---|---|---|
| **Install** | **Yes.** It finishes completely before it says it's done | Puts every component on disk: the runtime, the web UI, the document runtime (Office renderer, managed Node and Python packages) and the `clio-kit` launcher. Writes the manifest, then runs the "can it serve" check | 1 min target, 3 min at most |
| **First open** | Only until the window shows and accepts a message | Window shown, backend started, the configured model reachable, the default agent ready | Window at once; first message accepted within 6 s |
| **Setup** | **No.** It runs in the background after the window is up | Everything Clio prepares or checks for itself at runtime | Each item reports its progress and has its own budget |

**The TUI is no longer installed.** It hasn't been worked on in months. Installs stop downloading it
(today the script fetches `clio-tui-*` from the release and the source mode builds it with Go), and the
desktop app and the web UI are the front ends. Whether releases keep building it is a separate decision.

**Install is fast because it's prebuilt, not because it's partial.** Today it builds or resolves its
components on the user's machine, one after another: a Python environment from PyPI, then the document
packages and the Office renderer, then the science runtime. In the target, all of those come prebuilt
inside the per-OS runtime artifact. The install downloads or carries them, unpacks and checks them,
in parallel where the steps are independent.

**Setup installs only what a loaded agent needs, and nothing else.** It never installs Clio itself:
no runtime component, no `npm` or `pnpm`, no global packages. Those belong to install. The one install
work setup does is preparing the MCP servers of the plugins an agent uses, when that agent is loaded:
- **Not at install, not at startup for every plugin.** Plugins declare MCP servers, and most people use
  a few agents. Building every plugin's servers up front costs time and disk for tools nobody calls, and
  the cost grows with every plugin added.
- **When an agent is loaded, its plugins' servers are prepared.** Loading an agent (opening a
  conversation with it, or the default agent at first open) starts a setup item per server it needs that
  isn't prepared yet. The item is visible: "Preparing the hdf5 tools", with progress, then ready, or
  failed with the cause and the fix.
- **Prepare once, start every time.** Preparing (downloading and building the server's environment) is
  done once per server version and recorded. Starting a prepared server is cheap and happens each
  session. The two have separate time limits, so a slow first download never looks like a server that
  failed to start.
- **Only the requests that need those tools wait.** The conversation opens and accepts messages at once.
  A turn that calls a tool from a server still being prepared waits for that server, says so, and runs
  when it's ready. Turns that don't use it never wait.
- **In parallel, with a time limit each.** Servers are prepared and started concurrently. One slow or
  broken server never holds up another.
- **Status and retry.** Each server shows preparing, ready or failed in the app, and a failed one can be
  retried without restarting Clio.
- **Shipped servers are already prepared.** Servers of the plugins that ship with Clio (today `web`) are
  built into the runtime artifact, so the default agent never waits on this.

How the others do it:
- **Claude Code:** an enabled plugin's MCP servers connect when a session starts, in the background. A
  request waits only when it needs a server that is still connecting. Each server shows connected,
  needs authentication or failed, with a reconnect action. `MCP_TIMEOUT` bounds startup.
- **Codex:** servers in `config.toml` or bundled with a plugin start when Codex builds its tool catalog.
  `startup_timeout_sec` (10 s) bounds startup; optional servers get a short grace period and a server
  marked `required` fails startup if it can't initialize.
- **Hermes Agent:** servers in `config.yaml` start at startup and their tools are registered then, with
  `connect_timeout` and per-server `enabled`.
- **What none of them do:** none has a separate prepare step. A server configured as `npx -y ...` or
  `uvx ...` downloads and builds itself inside its startup timeout on first use, so a slow first download
  shows up as a server that failed to start. Clio's split between prepare and start is the fix for that,
  and it is the case Clio hits today.

How much this matters today: the default agent runs one MCP server, `web`, which is already built into
the runtime. The installer no longer installs the whole clio-kit science runtime, only the small
`clio-kit` launcher. Eight marketplace plugins declare servers (adios, geo, hdf5, ndp, pandas, parquet,
plot, spotter), and only science agents use them. The launcher starts each one through `uvx`, which
downloads and builds the server's environment inside its start, invisibly. Moving that into a visible,
recorded prepare step when a science agent is loaded is the change. It is small today, but the rule has
to hold at 15 servers and beyond.

**Setup is what runs in the background:** what Clio prepares or checks once it's running.

| Item | Needed for | Until it's ready |
|---|---|---|
| Restoring conversations and tasks from clio-core | Opening an older conversation | A new conversation starts at once; older ones open as they load |
| Preparing a loaded agent's plugin MCP servers | Calling that server's tools | The conversation works; a turn that calls those tools waits for that server |
| Provider sign-in check and model catalog refresh | The model picker's full list | The configured model works; the list fills in |
| Marketplace update check | Telling the person a new plugin version exists; installing it is a separate, explicit action | Runs at low priority |
| Cache pruning | Disk use | Runs last, at low priority |

Rules for setup:
- **Visible.** Each item shows in the app: preparing, ready, or failed with the cause and the fix.
- **Never silent.** A failure is shown where the person will look, not only written to a log.
- **Never in the way.** A first conversation never waits for an item it doesn't use.
- **Block only when needed, and only that request.** Nothing blocks up front. A request that needs an
  unfinished item waits for it and then runs; it doesn't fail, and the rest of the app stays usable.
  Opening an older conversation waits for that conversation alone; switching to a model whose provider
  hasn't been checked waits for that check. The configured model is checked during first open, so the
  first message never waits on setup. A wait says what it is waiting for, has a time limit, and if the
  item fails, that request shows the cause and the fix.
- **In order of likely use.** What a first conversation is likely to need is prepared first.
- **Resumable.** If Clio closes mid-setup, the next start continues where it stopped.
- **Prepared once.** Done items are recorded and never redone at boot. A plugin upgrade prepares its
  servers again, once.

The CI matrix measures each stage separately: install time, time to first window, time to first
accepted message, and time to each setup item.

## Three paths, one pipeline

There are three official ways to get Clio, and they are equals. Each must work and be fast on every OS:

| Path | Who | Notes |
|---|---|---|
| **Installers** (MSI/NSIS, DMG, deb/rpm, AppImage) | Desktop users | The desktop app and Clio in one download |
| **From source** | Developers, and **macOS users while installers are unsigned** | Must give the same result as an installer, not a developer-only setup |
| **Script** (`curl \| bash`, `irm \| iex`) | Servers, HPC nodes, command-line users, and every SSH deployment | The desktop app deploys to a remote host by running this there |

What makes them equals is that they differ in exactly one step: where the runtime comes from. The runtime
is Clio's backend with its own Python, the locked packages and the web UI. Everything after that
step is the same code on every path and every OS:

| Step | Installer | From source | Script |
|---|---|---|---|
| 1. Get the runtime | Carried inside the installer | **Built from the checkout**, with the same builder CI uses for releases | Downloaded: the release artifact for this OS and CPU |
| 2. Install it | Shared | Shared | Shared |
| 3. Record it in the install manifest | Shared | Shared | Shared |
| 4. Check it (start, wait until it can serve, stop) | Shared | Shared | Shared |
| 5. Front end | The desktop app, in the installer | **The desktop app built locally** (Tauri) against the runtime from step 1, when asked for | The web UI; the desktop app when asked for |

So a fix or speedup in any of steps 2–4 reaches all three paths on all three OSes at once. Step 1 is the
only place a path differs, and even there the installer and the script use the **same artifact**, and the
source path **builds that artifact**, rather than producing a different kind of install.

The commands, the same on every OS:

```
install                  # Clio: backend and web UI. Servers, HPC and SSH deployment use exactly this
install --desktop        # the same, plus the desktop app, on the same backend
install --source         # step 1 builds the runtime from source: clones, or uses the checkout it runs from
install --source --desktop   # also builds the desktop app locally: the macOS path today
install --dev            # the latest pre-release instead of the latest release
install repair           # finish or fix an interrupted or broken install
install uninstall
```

The installers run the same program with the runtime they carry. An SSH deployment is plain `install` on
the remote host. The in-app first-run install runs release mode; it never builds from source.

Contributor work in a checkout (`uv sync`, `uv run clio-agent serve`) stays as it is: that is editing Clio,
not installing it. It runs the same startup code, so the startup work below applies to it too.

### macOS

Until the macOS installers are signed, `install --source --desktop` is how Mac users get the desktop
app. An app built on the user's own machine is not quarantined, so Gatekeeper doesn't block it. That
makes the source path a release-quality path on macOS: the same budgets and CI coverage as an
installer. It also covers Intel Macs, which today get only the attach-only app, with no backend
included.

## Where we are

### Three paths, three different runtimes

Today each path produces a different kind of Clio:

| Path | Runtime | Python | How the desktop app finds the backend |
|---|---|---|---|
| Installers | A portable runtime built in CI by `build-gact-runtime.sh`/`.ps1`. **Windows**: packed, then unpacked at install. **Linux deb/rpm and Apple Silicon DMG**: carried as a directory. **AppImage and Intel DMG**: none (attach-only) | 3.14 | The bundled runtime manifest |
| From source, contributors | `uv sync` in the checkout | 3.14 | Not at all: a locally built desktop app has no backend and falls back to the in-app install |
| From source, in-app or `CLIO_REF` | The script's second mode: clone, `uv sync`, Go build of the TUI, with git, go and bash required | 3.14 | Hard-coded search paths |
| Script | A virtual environment built from PyPI wheels at install time, wiped on every reinstall | 3.13 | Hard-coded search paths, which have drifted (below) |

So the layout, the Python version, how Clio is started and how it's found all differ by path. A fix to
any of them has to be made up to four times. macOS CI already proves the target shape: it builds the
portable runtime from source with `build-gact-runtime.sh`, then builds the desktop app around it with
Tauri (`macos-bundle-smoke.yml`).

### Install code

The install logic exists many times over, in five languages:

| What | Implementations | Lines |
|---|---|---|
| Install the backend | `install/install.sh`, `install/install.ps1` | 312 + 322 |
| The `clio` launcher | `install/clio` (bash), `install/clio.ps1` | 520 + 363 |
| Build the packed runtime | `install/build-gact-runtime.sh`, `.ps1` | 286 + 400 |
| Desktop install, runtime pack, first run, launcher | gact-tui `desktop/src-tauri` (Rust, NSIS), `desktop/sidecar-launcher` (Go) | about 5,500 |
| SSH deployment around `install.sh` | gact-tui `ssh_*.rs`, `remote_lifecycle.rs`; clio-agent `gact/infrastructure/clio_agent_deploy.py` and friends | about 5,000 |
| Desktop app only | `install/desktop.sh`, `install/desktop.ps1` | 100 + 63 |
| Uninstall | `install/uninstall.sh`, `install/uninstall.ps1` | 138 + 145 |

"Where is Clio installed" is decided in four separate places with four copies of the rules:
`install.sh`, `install.ps1`, the desktop launcher (`resolve_candidates.go`, which still looks in the old
`%LOCALAPPDATA%\clio\...` prefix while `install.ps1` installs to `%LOCALAPPDATA%\clio-agent\data\app`),
and the deploy shell embedded in `clio_agent_deploy.py` (which in one case finds the install by parsing a
running process's command line).

### What CI checks today

- The **script path is never run in CI**, on any OS. The "installer scripts" job in `clio-bundles.yml`
  only uploads the scripts to the release. A script install that breaks, or takes 15 minutes on Linux,
  is found by users.
- Packaged desktop startup is smoke-tested on Linux, Windows and macOS (`desktop-bundle-smoke.yml`,
  `macos-bundle-smoke.yml`). The Windows job records the installer's duration, but nothing fails on it.
- No job runs a from-source install, an SSH deployment, an upgrade from the previous release, or a
  repair after an interrupted install. No job has a time limit on install, first start or restart.

### Failures seen

From [install-and-startup-paths.md](install-and-startup-paths.md):

- **Windows source mode needs bash.** It picks up WSL's `bash.exe` and fails when the WSL distro is stopped.
- **An interrupted install blocks the retry** ("installation already exists").
- **The desktop launcher can't find a fresh install** (the path drift above).
- **A source-built desktop app installs from source on first run** (`branding/clio/brand.json` sets `install.ref: develop`).
- **Running the script and the desktop installer gives two backends.**

### Startup, measured

Measured on 2026-10-25 against the release code, on a store with about 400 conversations. Every
startup function was timed directly; there were three agent-only boots plus one with a browser holding
a saved connection.

| Step | Time | Problem |
|---|---|---|
| Python start and imports | 3.6 s | Heavy libraries load before they are needed (#702) |
| Port open, health green | 4.7 s | Health says ready; nothing can be served yet |
| Restore task history | 30–33 s | Waking conversations with unread mail loads their whole histories; one load alone took 26 s |
| Transcript import check | 3.6–4.3 s | Runs on every boot and reads all 510 conversations |
| Open-task cleanup | 1.9–3.7 s | Loads whole conversation trees; grows with open work |
| Default marketplace check | 20–24 s, in parallel | Reinstalled on every boot: running spotter writes its Python environment into the installed copy, so it never matches the shipped one, and each reinstall deletes that environment |
| MCP cache cleanup | 0.1–1.4 s, in parallel | Walks the whole cache on every boot, can only evict environments that hold nothing today, and counts linked files several times over. Grows with every MCP server |
| **Usable** (conversations can be listed) | **44–49 s** | |

Two more problems:
- **The UI fails on a normal start.** If it opens while the agent is starting, it shows the service as
  unavailable with a connection error. It never recovers on its own, although its own background check
  reaches the agent at 33, 62 and 93 s.
- **Shutdown sometimes takes 1–3 minutes**, against 3–4 s normally. A restart in that window can collide
  with the old process (see #1401).

## Target architecture

### 1. One runtime artifact, one builder

The runtime is one artifact per OS and CPU: one Python version, the locked packages and the web UI, with the same layout everywhere. One builder makes it, from a checkout and the lock file:
- **CI** runs the builder to make release artifacts for every OS and CPU, Intel Mac and Linux AppImage
  included, so no installer is attach-only.
- **`install --source`** runs the same builder on the user's machine.

The portable runtime that installers carry today is the starting point. Its two builders
(`build-gact-runtime.sh` and `.ps1`) become one. Whether the artifact is compressed into a single file
(as on Windows today) or carried as a directory becomes a build option, the same on every OS, not a
Windows-only path.

### 2. One installer program

One program does steps 2–4 on every OS, and step 1 for the script and source paths:
- **Installers** carry it next to the runtime and run it, in place of the NSIS hooks and the desktop
  app's own unpacking, directory swap and runtime-stop code.
- **From source**, it runs the builder, then installs the result; with `--desktop`, it builds the
  desktop app with Tauri around that runtime.
- **The script's** `install.sh` and `install.ps1` become bootstraps of a few lines each: detect the OS
  and CPU, download the program, check its checksum, run it.
- **SSH deployment** runs it on the remote host.

Nothing else installs Clio. The program also replaces:
- the two `clio` launchers (`start`, `stop`, `restart`, `status`, `logs`, `doctor`);
- the desktop sidecar launcher;
- both uninstallers;
- the install half of SSH deployment.

### 3. One install record

The installer writes a versioned manifest in one OS-standard location: install root, version, mode,
components, runtime paths, the result of the last check, and step timings. Every consumer reads it and
nothing guesses paths: the desktop launcher, `clio doctor`, the agent itself and SSH deployment
(which reads it back over SSH). The desktop's bundled runtime manifest (gact-tui #311) becomes this
manifest. When the desktop app and the CLI are both installed, the manifest names the one backend they
share.

### 4. Steps that resume

The install is a list of idempotent steps. Each step records its result in the manifest. A failed step
reports a typed reason and the fix. Running the installer again, or `install repair`, continues from the
first unfinished step instead of refusing ("already exists") or wiping everything. Upgrades install the
new runtime next to the old one and switch over atomically, so a failed upgrade leaves the working
version in place (generalizing the desktop's `installer_dir_swap`). Settings and tokens are never
touched by an upgrade (#1777).

### 5. One shared cache

One cache, owned by Clio and shared by install and setup, holds packages, plugin environments and MCP
server environments. Plugins
never write into installed copies, so installed content stays identical to what was shipped and needs no
re-checking at boot. Cache size is bounded by uv's own pruning, run outside startup.

### 6. Every install ends with a real check

The last step starts Clio, waits until it can serve a request (not just until the port opens), and stops
it. The result is recorded in the manifest and shown to the user. This is #906's environment check, run
where it matters.

### 7. Startup that is correct, then fast

- **Honest readiness.** Health reports `starting` with the steps still running, then `ready`. Nothing
  claims to be ready before it can serve.
- **The UI never shows a failure for a normal start.** A saved service that is starting shows that it
  is starting, the UI keeps checking, and it opens the workspace by itself when the agent is ready. A
  real failure, after a bounded wait, names the cause and what to do.
- **Usable in about 5 s.** The server accepts requests as soon as it binds. Restoring history, loading
  plugins, discovering providers and starting MCP servers run in the background, in parallel. A message
  sent before the step it needs has finished is queued and runs when that step is done.
- **Startup reads small records, never whole histories.** Waking conversations with unread mail uses the
  mail records; a conversation's history loads only when it is opened. The transcript import is marked
  done once and skipped afterwards. Open-task cleanup reads task records, not trees.
- **Nothing is reinstalled at boot.** With rule 5 above, the shipped marketplace is never modified, so the
  boot check is a version comparison.
- **Heavy libraries load on first use, and MCP servers start in parallel** (#702).
- **Shutdown finishes in seconds** and always releases the port and the store (#1401).

## Quality bar

These are proposed numbers, to be confirmed once phase 0 has measured today's baseline on every OS. The
CI matrix fails the build when one is exceeded.

| Measure | Budget |
|---|---|
| Fresh install, release mode, from a prebuilt artifact | 1 min target, 3 min maximum, on Linux, macOS and Windows |
| First window after launch | Immediate (under 2 s) |
| Setup, all items | Each item reported; total within 5 min on a normal connection |
| Upgrade from the previous release | 1 min |
| Repair after an interrupted install | Completes; never asks the user to delete anything |
| Start to UI usable (can accept a message) | 6 s |
| Start to fully ready, reference store (400 conversations, 15 MCP servers declared) | 15 s |
| Restart | Same as start |
| Shutdown | 5 s |

## CI matrix

Built first, so it measures today's code before anything changes, and every later change is judged
against it.

- **Operating systems:** Ubuntu, macOS and Windows runners. Add ARM where the runners exist.
- **Paths:**
  - **Installers:** on Linux (deb/rpm and AppImage) and Windows, and on macOS once signing is in place;
    built unsigned for CI before that.
  - **From source:** `install --source` and `install --source --desktop`, on all three OSes. On macOS
    this is a release-quality path, on both Apple Silicon and Intel runners.
  - **Script.**
  - **SSH deployment** to a fresh container.
- **Scenarios:**
  - a fresh install;
  - first start;
  - a restart with the reference store;
  - an upgrade from the previous release;
  - a repair after a deliberately interrupted install;
  - an uninstall that leaves nothing behind.
- **Measurements:** the installer's own step timings from the manifest, plus the startup step timers used
  for the measurements above, made a permanent part of the server's startup trace.
- **When:** on every pull request that touches install or startup code, and nightly on everything.
- **Fixture:** a reference store generated in CI with about 400 conversations, open tasks, unread mail
  and 15 declared MCP servers, so startup is measured at a realistic size.

## Phases

### Phase 0: measure everything

- The CI matrix running against today's code, reporting without failing.
- Startup step timers in the server's own startup trace.
- **Deliverable:** a baseline table per OS and path. This also confirms or corrects the budgets above.

### Phase 1: never fails

- The install manifest. The launcher, desktop app and SSH deploy read it, and their path-guessing
  code is deleted.
- Resumable steps, `install repair`, and atomic upgrades.
- Typed reasons for every step that can fail; no warn-and-continue.
- Windows needs no bash.
- Honest readiness states. The UI waits for a starting service and uses clear wording.
- Shutdown cause found and fixed (#1401).
- Plugin MCP servers prepared when an agent that uses them is loaded: a visible, recorded setup item,
  separate from starting the server, with status and retry. `uvx` no longer builds environments inside
  a server's start.
- **Budgets enforced for:** install success, repair, upgrade and shutdown.

### Phase 2: one pipeline

- One runtime builder, run by CI for every OS and CPU (Intel Mac and AppImage included, so no installer
  is attach-only) and by `install --source`.
- The installer program and the bootstraps.
- Installers, source builds (with `--desktop` building the app locally) and SSH deployment all call it.
  The in-app first run uses release mode.
- In the same pull requests, delete the replaced scripts, launchers, runtime builders and deploy code.

### Phase 3: fast

- The per-OS runtime artifact, and parallel install steps.
- **Startup work:**
  - background, parallel boot steps, with queued messages;
  - mail-record wake-up;
  - a marketplace that is never modified, and the shared cache;
  - the one-time transcript import;
  - lazy imports and parallel MCP starts.
- Remove the boot-time MCP cache cleanup.
- **Budgets enforced for:** time.

### Phase 4: one set of docs

- One `INSTALL.md` describing the commands above.
- Close the issues this plan covers.

## Issues this plan covers

| Issue | Covered by |
|---|---|
| clio-agent #702, cold start (serial MCP spawning, lazy heavy imports) | Startup, phase 3 |
| clio-agent #1232, lazy mounting, non-blocking discovery, launcher lock, orphan reaping | Startup, phases 1 and 3; re-check what is left after the plugin rework |
| clio-agent #1217, first-turn blueprint resolve | Re-measure; blueprints were removed by the plugin rework |
| clio-agent #1401, Windows shutdown leaves runtime processes | Phase 1 |
| clio-agent #1777, in-app upgrade broke a saved access token | Phase 1 (upgrades never touch settings) |
| clio-agent #906, boot-time environment check | Architecture section 6 |
| gact-tui #311, self-describing runtime manifest | Architecture section 3 |

## Decisions needed

1. **What the installer program is written in.** Go is the proposal:
   - It ships as a single binary per OS, which runs before any Python exists.
   - Releases already build Go for every OS (the desktop launcher).
   - It can replace both launchers.
   - It removes the need for bash on Windows.

   The alternative is Python run through uv. That keeps everything in one language with the backend, but
   needs uv before anything else can run.
2. **The budgets above.**
3. **Whether every installer carries the runtime,** or some download it on first run.
4. **macOS signing:** owner and date, so macOS installers can join the matrix.
5. **The TUI:** it is no longer installed; decide whether releases still build and publish it.
