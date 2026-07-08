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
`.msi`/`.exe` (Windows), `.dmg` (macOS), `.deb`/`.AppImage`/`.rpm` (Linux). Bundles
clio-agent — nothing else to install.

## f) From source (contributors)
```sh
git clone --recurse-submodules https://github.com/iowarp/clio-agent
cd clio-agent && uv sync --extra optimizers --extra argonne
uv run src/clio_agent/ui/cli.py        # or: uv run clio-agent-gact
```

## ⚠️ Running more than one at once
The CLI (`clio` / `clio --web`) and the desktop app each spawn a gact server and default
to the **same port + the same `~/.config/clio-agent/` state dir**. Running two
simultaneously can clash on the port and risk concurrent-write corruption / version skew.
Use one at a time, or a different `CLIO_PORT` (desktop supervisor) / `--port`
(raw server) + state dirs. Tracked in
[#698](https://github.com/iowarp/clio-agent/issues/698).
