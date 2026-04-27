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

Installs to `$HOME\AppData\Local\clio`, drops `clio.cmd` into
`$HOME\AppData\Local\Microsoft\WindowsApps` (already on PATH on
Windows 10/11).

## What gets installed

- `clio-agent` (server, Python) — the v0.3.1 release
- `gact-tui` (TUI binary, Go) — the v0.2.1 release
- `clio` launcher — boots the server on `:17800` if not already running,
  then attaches the TUI

## Prerequisites

The script bails with install instructions if any of these are missing:

- `git`
- [`uv`](https://astral.sh/uv) — Python package manager
- `go` 1.26+

## Pinning a specific version

```sh
CLIO_REF=v0.3.1 GACT_REF=v0.2.1 \
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh
```

## What `clio` does on launch

1. Probes `:17800/v1/health`. If CLIO isn't running, starts it in the
   background (logs to `$CLIO_PREFIX/clio-server.log`).
2. Sets `GACT_BACKEND=http://127.0.0.1:17800` and runs `gact`.
3. The TUI pops the LM-provider config modal on first connect — pick a
   preset (Meridian / Anthropic / OpenAI / OpenRouter / LM Studio /
   Ollama), paste an API key if needed, save.

## Mid-session provider swap (no env vars)

`Ctrl+S` → Settings → Model → `Change provider…` → modal opens.
