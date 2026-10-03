# Installing & running CLIO

CLIO is **one engine + three frontends**: `clio-agent` (the Python core + the gact
server) is the brain; the **TUI**, **web**, and **desktop** are frontends that talk to a
gact backend. Every pathway below is "get clio-agent running + a frontend on it."

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
(Linux). Bundled installers ship clio-agent, so there is nothing else to install. Installers
without `-bundled` in the name, including every Linux `.AppImage` and the Intel macOS
`.dmg`, are attach-only: they connect to a clio-agent you install and run separately.

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

CLIO intentionally pins DSPy, FastMCP, and FastMCP Tasks prereleases and the tested
stable LiteLLM release. Registry-backed uv tool resolution requires those exact
prereleases as explicit roots, avoiding global admission of unrelated prereleases.
For a persistent backend-only installation, use `uv tool` (not the
ephemeral `uvx` / `uv tool run` path):

```sh
uv tool install --with dspy==3.4.0 --with fastmcp==4.0.0b5 --with fastmcp-slim==4.0.0b5 --with fastmcp-tasks==4.0.0b5 clio-agent==0.9.4.24
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

Reinstall replaces executable payloads while retaining sessions and user data.
Remote bootstrap also protects data when it must invoke an older release's
installer. Desktop gives fresh managed Agents their own `CLIO_AGENT_HOME`;
existing `clio-user` installations remain readable until migration. Core host
coordination is shared at native `state/core-hosts/<host>` (with legacy lookup)
because the Core daemon has one fixed endpoint per host.

## ⚠️ Running more than one at once
The CLI (`clio` / `clio --web`) and the desktop app each spawn a gact server and default
to the **same port**. Agents sharing a state directory must not run two writers.
Running two
simultaneously can clash on the port and risk concurrent-write corruption / version skew.
Use one at a time, or a different `CLIO_PORT` (desktop supervisor) / `--port`
(raw server) + state dirs. Tracked in
[#698](https://github.com/iowarp/clio-agent/issues/698).
