# One-line install

## Linux / macOS

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
```

Installs to `~/.local/share/clio` (override via `CLIO_PREFIX`), drops a
`clio` launcher into `~/.local/bin` (override via `CLIO_BIN_DIR`).

## Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

Installs to `$HOME\AppData\Local\clio`, drops `clio.cmd` + `clio.ps1`
into `$HOME\AppData\Local\Microsoft\WindowsApps` (already on PATH on
Windows 10/11).

## What gets installed (default: release mode)

- `clio-agent` (server, Python) — latest from **PyPI**, installed into a
  managed virtualenv at `$CLIO_PREFIX/clio-agent/.venv`
- `gact` (CLIO-branded TUI binary, Go) — matching `clio-agent`
  **GitHub Release** asset for your OS/arch (`clio-tui-linux-amd64`,
  `clio-tui-darwin-arm64`, `clio-tui-windows-amd64.exe`, …)
- `clio-kit` MCP tool launcher — provisioned via `uv tool install
  clio-kit==2.2.3` (idempotent) when `uv` is present. Marketplace packs
  launch their MCP servers through the installed `clio-kit mcp-server
  <name>` launcher; the installed tool (not `uvx clio-kit@…`) avoids the
  concurrent cold-cache ephemeral-env race and `uv cache prune` deleting
  envs under running servers ([astral-sh/uv#11694](https://github.com/astral-sh/uv/issues/11694)).
  Without `uv`, this step is skipped with a warning — install `uv`, then
  run `uv tool install clio-kit==2.2.3` and ensure the directory from
  `uv tool dir --bin` is on PATH. `clio doctor` flags a missing launcher.
- `clio` launcher — a small CLI that boots the server on `:17800` if
  not already running, attaches the TUI, and manages the server
  process (`clio start|stop|restart|status|logs|doctor|report`)
- `uninstall` script — dropped into the install prefix

### Prerequisites (release mode)

- `curl` (Linux/macOS) / `Invoke-WebRequest` (PowerShell — built in)
- `uv` (recommended) **or** Python 3.12+ with `pip`

That's it — no `git`, no `go`.

### Persistent backend-only install with uv

CLIO intentionally pins `dspy==3.3.0b1` for the retained ReActV2 runtime and
`litellm==1.91.3` as its tested stable provider boundary. Registry-backed uv tool
resolution requires the exact DSPy dependency as an explicit root, which keeps
unrelated dependencies on stable releases.

If you only need the long-running `clio-agent` backend, install it as a persistent uv
tool rather than using the ephemeral `uvx` / `uv tool run` environment:

```sh
uv tool install --with dspy==3.3.0b1 clio-agent==0.8.1
clio-agent serve
```

Use `uv tool upgrade clio-agent` for later upgrades. The full
one-line installer remains the supported path when you also want the CLIO-branded TUI.

## Source-build mode (track unreleased work)

Set `CLIO_REF` and optionally `GACT_REF` to a branch/tag to clone-and-build
instead of using PyPI/GitHub Releases:

```sh
# Linux / macOS — build clio-agent and its pinned GACT submodule from develop
CLIO_REF=develop \
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash

# Build clio-agent from source and override the GACT submodule ref
CLIO_REF=develop GACT_REF=develop \
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
```

```powershell
# Windows
$env:CLIO_REF = 'develop'
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

Source mode needs:

- `git` (always)
- `uv` (when `CLIO_REF` is set — Python deps from `uv sync`)
- `go` 1.26+ (source mode builds the CLIO-branded TUI)

If both repos are private and HTTPS clone returns 404, switch to SSH:

```sh
CLIO_REF=develop CLIO_GIT_PROTOCOL=ssh \
  bash <(curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh)
```

## Pinning a specific release

```sh
# Pin the PyPI version and matching clio-agent GitHub release tag
CLIO_VERSION=0.8.1 \
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
```

`CLIO_VERSION` is a PyPI version string (no `v` prefix). The installer downloads
`clio-tui-*`, launcher scripts, and uninstallers from the matching
`v<CLIO_VERSION>` GitHub release. `GACT_VERSION` remains as a legacy escape hatch
for testing an alternate TUI asset tag. Use `CLIO_INSTALLER_REF` only when you
intentionally need a different launcher-script ref. Leave release pins unset to
track "latest".

## Using `clio`

`clio` with no arguments is the original one-command UX: it ensures the
server is up, then attaches the TUI. The subcommands mirror
`gact agent *` so you can manage the backing server without hunting
PIDs:

| Command | What it does |
|---|---|
| `clio` | ensure the server is up, then attach the TUI |
| `clio start` | start the server (no TUI) |
| `clio stop` | stop the server (kills the whole process tree) |
| `clio restart` | stop, then start — clears stale/zombie launches |
| `clio status` / `clio ps` | PID, port, health |
| `clio logs [N]` | tail the server + gact stderr logs (default 40 lines) |
| `clio doctor` | check prerequisites and install layout |
| `clio sandbox status` | show the OS write-fence (sandbox) status row |
| `clio sandbox setup` | provision the OS write fence (one UAC prompt on Windows; idempotent) |
| `clio report` | print a diagnostics bundle to paste into a GitHub issue |
| `clio completion <shell>` | print shell completion |
| `clio uninstall [...]` | run the uninstaller |
| `clio help` | full help text |

The server runs detached. Its stdout/stderr land in
`$CLIO_PREFIX/clio-server.log` and `clio-server.err.log`; the TUI's
stderr (Go panics included) lands in `$CLIO_PREFIX/gact-stderr.log`.
`clio report` bundles all three plus version/environment info — that's
the thing to attach when filing an issue.

### OS write fence (sandbox)

CLIO can confine every process the agent spawns so it writes only inside
its workspace territory (the OS-level enforcement behind the advisory
file-policy). The fence is `@anthropic-ai/sandbox-runtime` (`srt`, an npm
package — `npm install -g @anthropic-ai/sandbox-runtime`, needs Node.js
>= 20.11). Check the status any time:

```sh
clio sandbox status      # mechanism + typed reason + next action
```

On **Linux / macOS** the fence activates automatically per process (no
setup) once `srt` is installed — Linux uses bubblewrap, macOS Seatbelt.
When `srt` is absent the fence falls back to the advisory floor (honestly
labeled; the row's `next_action` says what to install).

On **Windows** the fence is a one-time, self-elevating, idempotent setup:

```powershell
clio sandbox setup       # ONE UAC prompt — provisions the srt-sandbox principal + WFP filters
```

`setup` triggers a single UAC elevation and provisions the fence; every
per-session use afterward is unprivileged. Re-running `clio sandbox setup`
detects the already-provisioned state and no-ops with **no prompt**, so
it is safe to run twice. If `srt` (or Node.js) is missing, `setup` prints
a typed, guided pointer instead of an error — install it, then re-run.
`clio sandbox` is available on every install channel (the `clio-agent`
console entry point and the desktop `clio` launcher).

### Shell completion

```sh
# bash
clio completion bash >> ~/.bashrc

# zsh
clio completion zsh > "${fpath[1]}/_clio"

# PowerShell
clio completion powershell >> $PROFILE
```

## Uninstall

```sh
# Linux / macOS
clio uninstall            # or: ~/.local/share/clio/uninstall.sh
clio uninstall --purge    # also removes ~/.config/gact (config + agents.json)

# Windows (PowerShell)
clio uninstall
clio uninstall -Purge
```

Uninstall stops the server, removes the launcher, and deletes the
install prefix. Config under `~/.config/gact` is kept unless you pass
`--purge` / `-Purge`. Honors the same `CLIO_PREFIX` / `CLIO_BIN_DIR`
overrides as the installer.

## What `clio` does on launch

1. Probes `:17800/v1/health`. If CLIO isn't running, starts it detached
   (logs to `$CLIO_PREFIX/clio-server.log`) and records its PID in
   `$CLIO_PREFIX/clio-server.pid`.
2. Sets `GACT_BACKEND=http://127.0.0.1:17800` and runs `gact`.
3. The TUI pops the LM-provider config modal on first connect — pick a
   preset (OpenAI / Anthropic / OpenRouter / LM Studio / Ollama /
   ALCF Sophia / Metis), paste an API key if needed, save.

## Mid-session provider swap (no env vars)

`Ctrl+S` → Settings → Model → `Change provider…` → modal opens.
