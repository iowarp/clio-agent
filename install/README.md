# One-line install

## Linux / macOS

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh
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

## Private repo? Use SSH

Both repos are currently private. Until they're public, the HTTPS clone
in the one-liner above returns 404. Use SSH instead:

```sh
# Linux / macOS
CLIO_GIT_PROTOCOL=ssh \
  bash <(curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh)

# Windows (PowerShell)
$env:CLIO_GIT_PROTOCOL = 'ssh'
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

(Note: if `raw.githubusercontent.com/iowarp/clio-agent/...` itself
returns 404, the script file is unreachable too. In that case fetch the
script via `gh api repos/iowarp/clio-agent/contents/install/install.sh
--jq .content | base64 -d > install.sh && bash install.sh` — your `gh`
auth token gets you through.)

## What gets installed

- `clio-agent` (server, Python) — the `main` branch
- `gact-tui` (TUI binary, Go) — the `clio` branch
- `clio` launcher — a small CLI that boots the server on `:17800` if
  not already running, attaches the TUI, and manages the server
  process (`clio start|stop|restart|status|logs|doctor|report`)
- `uninstall` script — dropped into the install prefix

## Prerequisites

The script bails with install instructions if any of these are missing:

- `git`
- [`uv`](https://astral.sh/uv) — Python package manager
- `go` 1.26+

## Pinning a specific version

The installer tracks the `main` / `clio` branches by default. Pin a
tag or another branch with `CLIO_REF` / `GACT_REF`:

```sh
CLIO_REF=v0.3.1 GACT_REF=v0.2.1 \
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh
```

`CLIO_REF` / `GACT_REF` accept any git ref — a tag, or a branch like
`develop`.

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
| `clio report` | print a diagnostics bundle to paste into a GitHub issue |
| `clio completion <shell>` | print shell completion |
| `clio uninstall [...]` | run the uninstaller |
| `clio help` | full help text |

The server runs detached. Its stdout/stderr land in
`$CLIO_PREFIX/clio-server.log` and `clio-server.err.log`; the TUI's
stderr (Go panics included) lands in `$CLIO_PREFIX/gact-stderr.log`.
`clio report` bundles all three plus version/environment info — that's
the thing to attach when filing an issue.

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
   preset (Meridian / Anthropic / OpenAI / OpenRouter / LM Studio /
   Ollama), paste an API key if needed, save.

## Mid-session provider swap (no env vars)

`Ctrl+S` → Settings → Model → `Change provider…` → modal opens.
