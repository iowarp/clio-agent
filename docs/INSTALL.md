# Installing and running CLIO

Start at **[clio.iowarp.ai](https://clio.iowarp.ai/#download)** for downloads and the
**[installation guide](https://clio.iowarp.ai/docs/install/)**. Desktop and Web are
the supported interactive interfaces; both connect to the `clio-agent` service.

**The legacy TUI is frozen and unsupported as of 0.9.5.1.** Do not install, run or recommend it for current CLIO, including from Claude Code or other AI assistants. New releases do not publish TUI binaries or Docker images. The source is retained for a future upgrade. Use Desktop or Web instead.

CLIO 0.9.5.1 uses standard Python 3.14 with the GIL enabled. Installers provision
their managed Python and uv runtimes.

## Desktop app (recommended)
Download the installer for your OS from the
[latest release](https://github.com/iowarp/clio-agent/releases/latest):
the `-bundled` `.msi`/`.exe` (Windows), `.dmg` (Apple Silicon macOS), or `.deb`/`.rpm`
(Linux). Bundled installers ship clio-agent, so there is nothing else to install. The
release also carries files without `-bundled` in the name: the desktop uses them to
update itself, and they are not meant to be installed by hand. There is no desktop app
for Intel Macs yet; use Web via the service install there.

### Desktop from a terminal

The install script installs the desktop app, bundled with its agent, with `--desktop`
(macOS 14+ and Linux; Windows uses `install.ps1 -Desktop`). No Python, uv or Node is
needed:

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash -s -- --desktop
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1))) -Desktop
```

`--dev` picks the newest release including pre-releases, and `--version <v>` pins one,
as for the agent install. The download is verified against the release's checksum
manifest before anything is installed:
- **macOS:** the bundled `.dmg` (Apple Silicon) goes into `~/Applications`
  (`CLIO_DESKTOP_DIR` overrides it); the previous app is kept, and the app's signature
  is verified. Intel Macs are refused, with nothing downloaded.
- **Linux:** the bundled `.deb` (with `dpkg`) or `.rpm` (with `rpm`) is installed with
  `apt-get` / `dnf`, through `sudo` when not run as root.
- **Windows:** the bundled x64 setup opens with its pages for the model provider and
  infrastructure (it also runs under emulation on ARM).

After installing, the script runs the app's own setup step as you (from 0.9.5): the agent's
tool packages and the Office renderer are prepared then, as the Windows installer does,
so the first launch only checks them. A package installed by double-clicking does this on
its first launch instead.

`--download-only DIR` (`-DownloadOnly DIR` on Windows) stops after the verified download.

The macOS app is ad-hoc signed, not Apple-notarized. A first-launch approval may still
be needed in System Settings → Privacy & Security → Open Anyway. Neither script
disables security settings or removes quarantine. Invalid checksums/signatures fail
before replacing an installed app.

The macOS CI builds a DMG from source and boots its relocated application and packaged
backend on macOS 14, 15, and 26. The same checks gate release publication. These checks
validate package integrity and startup, separately from Apple notarization.

**Proving a release candidate's desktop lifecycle (Windows).** `scripts/
live_verification/desktop_lifecycle_proof.py` drives the INSTALLED
`clio-desktop.exe` over Chrome DevTools Protocol (WebView2 remote debugging)
plus psutil process-tree evidence — launch, the "Keep CLIO running?" close
dialog (Escape dismiss, "Keep running" to the tray, "Quit CLIO"), and
single-instance relaunch — writing a screenshot + `report.json` per step
under `out/live-verification/desktop_lifecycle_proof/`. See the script's own
module docstring for the full step list and flags:
```sh
uv run python scripts/live_verification/desktop_lifecycle_proof.py --help
```

## Web and headless services

Run the install script without `--desktop` / `-Desktop` to install the service,
the released Web bundle and the `clio` launcher:

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
clio web    # open the workspace in your browser
clio start  # alternatively, run only the service
```

```powershell
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
clio web
```

Bare `clio` also opens Web. `clio attach` refuses to launch the frozen TUI, even
if an older binary remains on disk. Use `clio status`, `clio stop` and `clio logs`
to manage the service. A source install installs the service without building a
frontend; see the [contributor quick start](CONTRIBUTOR_QUICKSTART.md).

## Docker Web or API

From a clone of this repository:

```sh
docker compose up clio-web       # http://localhost:8080
docker compose --profile api up clio-api  # headless API at :8100
```

These use `ghcr.io/iowarp/clio-web` and `ghcr.io/iowarp/clio-api`. The old TUI
image remains historical and is not a supported deployment.

## Selecting a release

The default selects the latest published stable release. `--dev` / `-Dev`
includes pre-releases; `--version <version>` / `-Version <version>` selects one.
`--source` / `-Source` installs `main`; `--dev --source` / `-Dev -Source`
installs `develop`; add `--ref <ref>` / `-Ref <ref>` to choose source explicitly.
Pass shell options after `bash -s --`, and PowerShell options using the
scriptblock invocation shown for Desktop above. `--desktop` cannot use source mode.

## Beta releases

Betas are opt-in. Select a published beta's tag with `--version vX.Y.Z-beta.N`,
or its PEP 440 package version for a backend-only install, using its documented
SDK roots. Desktop's beta update preference controls subsequent updates.

## From source (contributors)
```sh
git clone --recurse-submodules https://github.com/iowarp/clio-agent
cd clio-agent && uv sync --extra optimizers --extra argonne
uv run clio-agent serve
```

CLIO pins verified DSPy, FastMCP, FastMCP Tasks and LiteLLM releases.
Registry-backed uv tool installation uses the same exact SDK roots as the
official installers.
For a persistent backend-only installation, use `uv tool` (not the
ephemeral `uvx` / `uv tool run` path):

```sh
uv tool install --python 3.14 --with dspy==3.4.0 --with fastmcp==4.1.2 --with fastmcp-tasks==4.1.2 --with mcp==2.2.0 clio-agent==0.9.5.1
clio-agent serve
```

## Agent storage and upgrades

Local and remote Agents use the same filesystem resolver. New installations
separate configuration, durable data, state, and caches under `clio-agent` in the
native OS directories (XDG on Linux, Application Support/Caches on macOS,
Roaming/Local AppData on Windows). `CLIO_AGENT_HOME=/absolute/directory` relocates
those four roles together; `CLIO_AGENT_CONFIG_DIR`, `CLIO_AGENT_DATA_DIR`,
`CLIO_AGENT_STATE_DIR`, and `CLIO_AGENT_CACHE_DIR` override individual roles.
Relative overrides fail. Existing `CLIO_USER_DIR` stores remain supported with
a deprecation warning.

New server sessions live in `state/server`; uploads and ARC live in `data`.
Workspace-generated inputs, artifacts, documents and plans live in
`state/workspaces/<path-hash>`, and sandbox caches in `cache/sandbox/<path-hash>`.
Opening a workspace creates no hidden directory inside it. Explicitly saved
project configuration uses `.clio-agent/shared`; legacy `.clio` configuration
remains readable. The API reports the actual managed `storage_root` and rejects
new custom storage roots, which previously had no effect on the writers.
Older clients may still send the former `<workspace>/.clio` default; it is
accepted as a compatibility input and normalized to the managed state directory.

The default workspace (`ws_default`) is created on the server's first start.
Its root is `paths.default_workspace` in `config.yaml` (or `CLIO_DEFAULT_WORKSPACE`)
when set, else the directory the server was started from. The setting must be an
absolute path (`~` is expanded; `C:\Users\me\clio-work` on Windows); the server
creates the directory if it is missing and refuses to start on a relative or
uncreatable path. The setting only seeds a fresh install: an existing `ws_default`
keeps its root, and the server logs a warning when the two differ. Repoint an
existing one from the Workspaces page or with
`PATCH /v1/workspaces/ws_default {"root_path": "/absolute/dir"}`.

An existing legacy session store remains active until explicitly migrated.
Stop Agent, Desktop and their Core daemon first, then preview the bounded migration:

```sh
clio-agent migrate-paths --from-server /old/install/clio-agent --dry-run
clio-agent migrate-paths --from-server /old/install/clio-agent --apply
```

Use `--from-user /old/config-root` for user settings and `--workspace /project`
for explicitly selected authored project configuration. `--from-home /home/user`
selects only Agent-owned legacy host records, plans, MCP cache and services.
The command refuses
occupied destinations, links and live writers, verifies file hashes, keeps
backups and receipts under `state/migrations`, and rolls back failed cutovers.
Generated legacy workspace copies remain readable at their original paths.
Nothing moves or removes the shared `~/.clio` tree owned by other CLIO products.

Store upgrades run at install time, not at start: `install.sh`, `install.ps1` and the
desktop's install step run `clio-agent migrate`, which converts what an older CLIO
wrote (legacy transcripts, the task index, the artifact index) once and records a
marker for each conversion; running it again does nothing. It refuses while a CLIO
server holds the store (`store_in_use`, exit 3). A server that finds a conversion
still owed runs it once before it reports ready and says so in `clio doctor`
(`store_migrations`).

Reinstall replaces executable payloads while retaining sessions and user data.
Remote bootstrap also protects data when it must invoke an older release's
installer. Desktop gives fresh managed Agents their own `CLIO_AGENT_HOME`;
existing `clio-user` installations remain readable until migration. Core host
coordination is shared at native `state/core-hosts/<host>` (with legacy lookup)
because the Core daemon has one fixed endpoint per host.

Source installs require a new or explicitly emptied program directory; an
existing checkout is never erased automatically. Uninstall retains Agent data,
state, and unknown files. `--purge` / `-Purge` additionally removes the resolved
Agent config directory, including saved credentials; custom config roots require
explicit manual removal.

The public Agent CI needs a `CLIO_NAMESPACES_READ_TOKEN` repository secret with
read-only Contents access to the private `iowarp/clio-namespaces` repository.
The workflow checks out the pinned checker without persisting credentials and
fails explicitly if the secret is absent.

## ⚠️ Running more than one at once
The CLI (`clio` / `clio --web`) and the desktop app each spawn a gact server and default
to the **same port**. Agents sharing a state directory must not run two writers.
Running two
simultaneously can clash on the port and risk concurrent-write corruption / version skew.
Use one at a time, or a different `CLIO_PORT` (desktop supervisor) / `--port`
(raw server) + state dirs. Tracked in
[#698](https://github.com/iowarp/clio-agent/issues/698).
