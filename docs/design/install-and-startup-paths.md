# Install and startup paths: current state and proposed unification

Status: findings, 2026-10-26. Nothing here is fixed yet. The plan built on these findings is
[install-and-startup-plan.md](install-and-startup-plan.md).
Observed by building `iowarp/gact-tui@develop` (`e58bbfe`) on Windows 11 with the
CLIO brand, launching the debug desktop app, and reading the installers.

## Summary

There is no single installer. There are five separate entry points, written
separately per OS, with different defaults and different prerequisites. Only one
combination (Windows, bundled desktop) is an optimized, fast install. The others
differ in what they install, how, and from where.

Proposal: one installer, `install`, with options:

```
install              # stable release: backend + TUI (+ web)
install --desktop    # the desktop app
install --dev        # latest non-stable (pre-release)
install --source     # build from source
```

## Current entry points

| # | Entry point | OS | Installs | Source of truth |
|---|---|---|---|---|
| 1 | `install.sh` / `install.ps1` (`curl \| bash`, `irm \| iex`) | Linux, macOS / Windows | `clio-agent` backend (PyPI), TUI (prebuilt release binary), web UI bundle (best-effort), `clio-kit`, launcher. **No desktop app.** | PyPI + GitHub Releases |
| 2 | `desktop.ps1` | Windows | Desktop app: downloads the **bundled** NSIS setup, verifies SHA256, runs it silently. The bundled setup carries its own `clio-agent` runtime. | GitHub Releases |
| 3 | `desktop.sh` | macOS only | Desktop app. Apple Silicon: bundled DMG. Intel: lite (attach-only) DMG. Needs macOS 14+. | GitHub Releases |
| 4 | Native installers (MSI/NSIS, DMG, deb, rpm, AppImage) | all | Desktop app. `-bundled` variants include the backend; the others (all AppImage, Intel DMG) are attach-only and need a separately installed `clio-agent` (`docs/INSTALL.md`). | GitHub Releases |
| 5 | Build the desktop from source (`pnpm ... tauri:build`) | all | A bare binary with no runtime. On first launch the app falls back to the in-app install below. | `gact-tui` repo |

Plus the **in-app first-run install**: when a managed-brand build finds no
backend, the app runs entry point 1 itself with `CLIO_REF=develop`, which puts
it in source-build mode (see below).

### Things no entry point does

- Install the desktop app and the CLI/TUI together: entry points 1 and 2/3 are
  separate, and the bundled desktop carries its own backend, so running both
  gives two backends.
- Install the desktop app on Linux from a script (`desktop.sh` refuses; use the
  bundled `.deb`/`.rpm`).
- "Latest pre-release": today you must pin one (`CLIO_VERSION=v0.9.5-beta.8`);
  `CLIO_REF=<branch>` is a source build, not a pre-release install.

## What `install.ps1` does (release mode, the default)

1. Needs `uv` or `pip` (pip also needs Python 3.13).
2. Creates a Python 3.13 venv under
   `%LOCALAPPDATA%\clio-agent\data\app\clio-agent\.venv` (wiped each run) and
   installs `clio-agent` plus pinned `dspy`/`fastmcp` from PyPI.
3. Runs `clio_agent.runtime.document_install --setup-protected-execution`
   (managed Python/Node packages, Office renderer, protected-execution check).
4. `uv tool install clio-kit==2.10.6` (warn-only on failure).
5. Downloads `clio-tui-windows-{amd64,arm64}.exe` as `gact.exe` from the latest
   release.
6. Downloads and unpacks the web UI bundle (`clio --web`), best-effort.
7. Installs the `clio.cmd`/`clio.ps1` launcher and an uninstaller.

No `git`, `go` or `bash` needed. Setting `CLIO_REF` (and optionally `GACT_REF`)
switches to **source-build mode**: `git clone`, `uv sync --python 3.14`, then
`bash ./scripts/build_clio_tui.sh` (a Go build). It also refuses to run if the
install directory already exists. The two modes install different Python stacks
(3.13 vs 3.14) and behave differently on reinstall (wipe vs refuse).
`install.sh` mirrors this for Linux and macOS.

## What happened on a source desktop build (Windows)

1. `pnpm build` and `tauri:build:debug`: fine (needs Rust, MSVC Build Tools,
   Node 20+, pnpm 9.15.9, and Go for the sidecar launcher).
2. First launch: the managed brand (`branding/clio/brand.json`) sets
   `install.ref: "develop"`, so the app runs
   `$env:CLIO_REF='develop'; irm .../install.ps1 | iex`, i.e. source-build mode:
   clone, `uv sync` (about 240 packages, a 145 MB `openai-codex-cli-bin`), Office
   renderer, Node/pnpm packages, Go build of the TUI. Slow, and none of it is
   needed to connect to a remote server.
3. **Failure 1, bare `bash`.** The TUI step runs `bash ./scripts/build_clio_tui.sh`.
   With WSL installed, `bash` resolves to the WSL launcher
   (`WindowsApps\bash.exe`); with the distro stopped it died with
   `HCS_E_CONNECTION_TIMEOUT`. Git Bash is not on PATH by default, and the
   script's `Have bash` check passes on the WSL stub.
4. **Failure 2, partial-install guard.** The retry stopped with "Source
   reinstall refused: the installation already exists" because the failed run
   left `%LOCALAPPDATA%\clio-agent\data\app` behind. Recovery was a manual rename.
5. **Failure 3, launcher path drift.** After a successful install the sidecar
   launcher still said "clio-agent-gact not found": it searches
   `%LOCALAPPDATA%\clio\clio-agent\.venv\Scripts\` but the installer writes to
   `%LOCALAPPDATA%\clio-agent\data\app\clio-agent\.venv\Scripts\`.
   `CLIO_AGENT_GACT_BIN` worked around it.

Workarounds used: Git Bash and Go first on PATH when launching the app;
`CLIO_AGENT_GACT_BIN` set; stale `data\app` renamed.

## Problems

1. **Five entry points, with OS-specific scripts each** (`.ps1`/`.sh`/native).
   Fixes and optimizations apply to one path and miss the others.
2. **The fast path is narrow.** The packed runtime (`gact-runtime.tar.zst`,
   `runtime_pack.rs`) is `cfg(windows)`-only. Linux bundles deb/rpm only (no
   bundled AppImage); macOS bundles Apple Silicon only.
3. **Desktop and CLI installs are separate** and can leave two backends.
4. **Source-build mode is a second implementation** of the install, with its own
   Python version, reinstall behavior, and prerequisites (git, go, bash).
5. **Windows depends on bash** in source mode. Windows should not require WSL or
   Git Bash; the TUI is a Go program and can be built with `go build` directly.
6. **Launcher search paths are hard-coded** and have drifted from the
   installer's prefix.
7. **Partial installs are not resumable.**
8. **A source desktop build defaults to the slowest path** (`install.ref: develop`).

## Proposal: one installer, options instead of paths

| Option | Meaning | Replaces |
|---|---|---|
| (none) | Latest stable: backend + TUI + web | `install.sh` / `install.ps1` release mode |
| `--desktop` | Desktop app (bundled where it exists); combinable with the above so there is one backend | `desktop.sh`, `desktop.ps1`, manual native installers |
| `--dev` | Latest non-stable (pre-release), no pinning by hand | `CLIO_VERSION=<beta>` |
| `--source` | Build from source (git, uv, go as needed; never bash on Windows) | `CLIO_REF` / `GACT_REF` |

Supporting changes:

- One implementation per step, shared across OSes where possible (a single
  entry that dispatches, rather than parallel `.sh`/`.ps1` logic).
- The in-app first-run install should call the same installer in release mode
  (not `CLIO_REF=develop`); source stays an explicit developer choice.
- Drop `bash` from Windows source builds (`go build`).
- Have the installer record its prefix so the launcher does not search
  hard-coded paths.
- Make the "already exists" case a repair or resume, not a hard refusal.
- Treat a prebuilt runtime as the one provisioning contract across all OSes
  rather than a per-OS fast path.

## Open questions

- Why does a managed-brand build ship with the in-app install fallback at all,
  when released lite builds are documented as attach-only?
- Plans for a bundled Intel macOS build, a bundled AppImage, and a Linux desktop
  script?
- Not tested: `desktop.ps1` end to end on this machine.
