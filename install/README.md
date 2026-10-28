# One-line install

## Linux / macOS

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
```

Installs to `~/.local/share/clio-agent/app` (macOS: `~/Library/Application
Support/clio-agent/data/app`; override via `CLIO_PREFIX`), drops a `clio` launcher into
`~/.local/bin` (override via `CLIO_BIN_DIR`). Add `--desktop` for the desktop app.

## Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

Installs to `$HOME\AppData\Local\clio-agent\data\app`, drops `clio.cmd` + `clio.ps1`
into `$HOME\AppData\Local\Microsoft\WindowsApps` (already on PATH on
Windows 10/11).

## What gets installed (default: release mode)

- `clio-agent` (server, Python) — latest from **PyPI**, installed into a
  managed virtualenv at `$CLIO_PREFIX/clio-agent/.venv`
- `gact` (CLIO-branded TUI binary, Go) — matching `clio-agent`
  **GitHub Release** asset for your OS/arch (`clio-tui-linux-amd64`,
  `clio-tui-darwin-arm64`, `clio-tui-windows-amd64.exe`, …)
- `clio-kit` MCP launcher — provisioned via `uv tool install clio-kit==2.10.6`
  (idempotent) when `uv` is present. This released version retains legacy runtime
  semantics until the paired shared-runtime release. Marketplace packs
  launch their MCP servers through the installed `clio-kit mcp-server
  <name>` launcher; the installed tool (not `uvx clio-kit@…`) avoids the
  concurrent cold-cache ephemeral-env race and `uv cache prune` deleting
  envs under running servers ([astral-sh/uv#11694](https://github.com/astral-sh/uv/issues/11694)).
  Without `uv`, this step is skipped with a warning — install `uv`, then
  run `uv tool install clio-kit==2.10.6` and ensure the directory from
  `uv tool dir --bin` is on PATH. `clio doctor` flags a missing launcher.

For local qualification of an unreleased shared-runtime CLIO Kit wheel, set
`CLIO_KIT_PACKAGE` to its absolute path before running the installer. Only this
explicit candidate path adds the `science` extra. Validate it with `clio-kit
runtime-info ndp geo pandas plot`; the released pin does not provide that contract.
- `clio` launcher — a small CLI that boots the server on `:17800` if
  not already running, attaches the TUI, and manages the server
  process (`clio start|stop|restart|status|logs|doctor|report`)
- `uninstall` script — dropped into the install prefix

### Prerequisites (release mode)

- `curl` (Linux/macOS) / `Invoke-WebRequest` (PowerShell — built in)
- `uv` (recommended) **or** Python 3.13 with `pip` for published releases

That's it — no `git`, no `go`.

This checkout's source installers and bundled runtime select Python 3.14 with the
GIL enabled. Release-mode installs and the backend-only examples below keep Python
3.13 because current published releases pin clio-core 2.2.x, which has no 3.14 wheels.

### Persistent backend-only install with uv

CLIO intentionally pins DSPy, FastMCP, and FastMCP Tasks prereleases and
`litellm==1.102.1` as its tested stable provider boundary. Registry-backed uv tool
resolution requires those exact prereleases as explicit roots, which keeps unrelated
dependencies on stable releases.

If you only need the long-running `clio-agent` backend, install it as a persistent uv
tool rather than using the ephemeral `uvx` / `uv tool run` environment:

```sh
uv tool install --python 3.13 --with dspy==3.4.0 --with fastmcp==4.0.0b5 --with fastmcp-slim==4.0.0b5 --with fastmcp-tasks==4.0.0b5 clio-agent==0.9.4.24
clio-agent serve
```

Use `uv tool upgrade clio-agent` for later upgrades. The full
one-line installer remains the supported path when you also want the CLIO-branded TUI.

To opt into CLIO v0.9.5-beta.8, replace the package pin above with
`clio-agent==0.9.5b8`. For the complete beta frontend/backend install, use
`install.sh --dev` (or `--version 0.9.5b8`). Beta selection is explicit;
the examples for the default channel continue to name the current stable release.

## The ways to install

| Command | Installs |
|---|---|
| `install.sh --desktop` | the desktop app, bundled with its agent (latest stable; `--dev`/`--version` as below) |
| `install.sh` | the latest stable release |
| `install.sh --dev` | the most recently published release, pre-releases included |
| `install.sh --source` | source at `main`, with what `main` pins (submodules, `uv.lock`) |
| `install.sh --dev --source` | source at `develop` |

```sh
URL=https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh
curl -fsSL $URL | bash                          # latest stable release
curl -fsSL $URL | bash -s -- --dev              # latest release, betas included
curl -fsSL $URL | bash -s -- --source           # main, from source
curl -fsSL $URL | bash -s -- --dev --source     # develop, from source
curl -fsSL $URL | bash -s -- --desktop          # the desktop app (Apple Silicon macOS, Linux)

# From your own clone: installs that checkout in place, on whatever branch it is
git clone --recurse-submodules https://github.com/iowarp/clio-agent.git
cd clio-agent && ./install/install.sh --source
```

```powershell
$u = 'https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1'
& ([scriptblock]::Create((irm $u))) --dev --source   # same options; -Dev -Source work too
```

- **Releases** come from PyPI, constrained by that release tag's own `uv.lock`, on the
  Python that release declares in `.python-version` (3.13 for releases before it).
  The release list is GitHub Releases: the default takes the highest stable version,
  `--dev` the most recently published release.
- **Source** uses `uv sync --locked` (a clone) or `uv sync` (your checkout, in place).
  It needs `git`. The terminal UI is built when `go` 1.26+ is installed, in the
  background while the other steps run (its log is printed if it fails); without
  `go` the installer says so and the rest of CLIO is installed. Running a clone
  install again updates that clone to the requested ref and repairs what an
  interrupted run left; a clone with local changes to tracked files, another origin,
  or other files in its place is refused, with the cause and the fix, and left as is.
- `uv` is installed into the launcher directory when it is missing.
- `clio` on PATH runs the installed release's own launcher with this installation's
  location, so any release finds its install.

**Pins** (deployments and CI): `--version <pypi version>` for a release,
`--ref <branch, tag or commit>` for source. The older spellings still work:
`CLIO_VERSION=<v>` is `--version <v>`, `CLIO_REF=<ref>` is `--source --ref <ref>`.
`GACT_REF` builds the TUI from another gact-tui ref (source only); `GACT_VERSION`
remains a legacy escape hatch for an alternate TUI release asset, and
`CLIO_INSTALLER_REF` for a different launcher-script ref.

If a repository is private and an HTTPS clone returns 404, use SSH:
`CLIO_GIT_PROTOCOL=ssh bash install/install.sh --source`.

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
