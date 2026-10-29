# Installing & running CLIO

CLIO is **one engine + three frontends**: `clio-agent` (the Python core + the gact
server) is the brain; the **TUI**, **web**, and **desktop** are frontends that talk to a
gact backend. Every pathway below is "get clio-agent running + a frontend on it."

CLIO 0.9.5 selects standard Python 3.14 with the GIL enabled for source builds,
installers, containers and bundled Desktop. The installer provisions its managed
Python and uv runtimes.

There are **4 install mechanisms** exposing **6 usage experiences** (a–f).

| # | Experience | Mechanism | Status |
|---|---|---|---|
| a | CLI / TUI | pip-or-uv install (script) | ✅ |
| b | No-install TUI | docker | ✅ |
| c | Local web UI | install script + `clio --web` | ✅ |
| d | Scaled / hosted web | docker (compose) | ✅ |
| e | Desktop app | native installer | ✅ |
| f | From source | git clone | ✅ |

## a) Install script → clio-agent + TUI
```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
clio          # ensure the server is up, attach the TUI
clio status   # pid / port / health   ·   clio stop / clio restart / clio logs
```
Installs `clio-agent` from PyPI + the `clio-tui` binary + the `clio` launcher. Windows:
`install.ps1`. Pin a version with `CLIO_VERSION=X.Y.Z`.

## Beta releases

Beta releases are opt-in. Select a published beta tag with
`CLIO_VERSION=vX.Y.Z-beta.N` for the complete installer, or its PEP 440 package
version `X.Y.ZbN` for a backend-only install. Use that beta's documented SDK roots.
Desktop's beta update preference controls later updates; the default install
channel selects published stable releases.

## b) No-install, Docker (TUI)

```sh
docker run -it --rm \
  -e CLIO_LM_API_BASE=http://host.docker.internal:1234/v1 \
  ghcr.io/iowarp/clio-tui:latest
```
The `clio-tui` image bundles the backend; `-it` gives the terminal UI a real tty.

## c) Local web UI — `clio --web`
After (a):
```sh
clio --web    # starts the agent in web mode and opens the browser
```
The gact server serves the web SPA **same-origin** (one process, no proxy). The install
script fetches the web bundle into `$CLIO_PREFIX/clio-agent/web`; override with
`CLIO_WEB_DIR=/path/to/web/dist`.

## d) Scaled / hosted web (Docker Compose)

```sh
docker compose up clio-web          # self-contained web UI → http://localhost:8080
docker compose --profile api up     # headless backend (API/SSE) → :8100 for scale-out
```
Uses the published `ghcr.io/iowarp/clio-{web,api}` images (no build). Override `CLIO_LM_*`
to point at your model provider.

## e) Desktop app
Download the installer for your OS from the
[latest release](https://github.com/iowarp/clio-agent/releases/latest):
the `-bundled` `.msi`/`.exe` (Windows), `.dmg` (Apple Silicon macOS), or `.deb`/`.rpm`
(Linux). Bundled installers ship clio-agent, so there is nothing else to install. The
release also carries files without `-bundled` in the name: the desktop uses them to
update itself, and they are not meant to be installed by hand. There is no desktop app
for Intel Macs yet; use the install script without `--desktop` there.

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

## f) From source (contributors)
```sh
git clone --recurse-submodules https://github.com/iowarp/clio-agent
cd clio-agent && uv sync --extra optimizers --extra argonne
uv run src/clio_agent/ui/cli.py        # or: uv run clio-agent serve
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
