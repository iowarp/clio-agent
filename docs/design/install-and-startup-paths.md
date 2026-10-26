# Install and startup paths: findings and optimization proposal

Status: findings, 2026-10-26. Nothing here has been fixed yet.
Observed building `iowarp/gact-tui@develop` (`e58bbfe`) on Windows 11 with the
CLIO brand and launching the debug desktop app.

## Summary

There are two ways to get CLIO Desktop running: a **binary download** (double
click) and a **source build**. Only part of the binary path is optimized. The
rest falls through to the shell installers (`install.ps1` / `install.sh`) in
source-build mode, which is slow, needs a toolchain, and is fragile.

## The two paths, by OS

| | Windows | macOS | Linux |
|---|---|---|---|
| **Binary** | MSI/NSIS. *Bundled* variant ships a compressed runtime (`gact-runtime.tar.zst`) expanded by `runtime_pack.rs`: the optimized path. *Lite* has no runtime. | DMG. Bundled for **arm64 only**; x86_64 is lite only (excluded in `.github/desktop-bundle-matrix.json`). | deb, rpm, AppImage (x86_64, arm64). Bundled is **deb/rpm only**; AppImage is lite. |
| **Source** | Bare `tauri build`, no runtime. First run falls back to `install.ps1`. | Same, falls back to `install.sh`. | Same, falls back to `install.sh`. |

*Lite* and *source* builds both boot with no backend, get exit 2 from the
sidecar launcher ("routing to first-run install"), and run the upstream
installer from inside the app.

The runtime-pack fast path is `cfg(windows)`-only (`runtime_pack.rs`). Linux and
macOS bundled builds ship the runtime as plain resources instead, so the
"optimized installer" work does not carry over.

## What happened on a source build (Windows)

1. `pnpm build` and `pnpm --filter @clio/desktop tauri:build:debug`: fine
   (needs Rust, MSVC Build Tools, Node 20+, pnpm 9.15.9, Go for the sidecar).
2. First launch: the app runs
   `powershell -Command "$env:CLIO_REF='develop'; irm .../install.ps1 | iex"`.
   The CLIO brand sets `install.ref: "develop"` (`branding/clio/brand.json`),
   and `CLIO_REF` switches `install.ps1` into **source-build mode**: git clone,
   `uv sync` (Python 3.14, ~240 packages, a 145 MB `openai-codex-cli-bin`),
   Office renderer, Node/pnpm document packages, then a Go build of the TUI.
   Release mode (PyPI plus prebuilt TUI) is the script's documented default.
3. **Failure 1 - bare `bash`.** The TUI step runs `bash ./scripts/build_clio_tui.sh`.
   On a machine with WSL, `bash` resolves to the WSL launcher
   (`WindowsApps\bash.exe`) and, with the distro stopped, died with
   `HCS_E_CONNECTION_TIMEOUT`. Git Bash is not on PATH by default. The script's
   `Have bash` check passes on the WSL stub, so it does not catch this.
4. **Failure 2 - partial install guard.** The retry stopped with
   "Source reinstall refused: the installation already exists", because the
   failed run left `%LOCALAPPDATA%\clio-agent\data\app` behind. The folder had
   to be moved aside by hand.
5. **Failure 3 - launcher path drift.** After a successful install the sidecar
   launcher still reported "clio-agent-gact not found". `sidecar-launcher`
   searches `%LOCALAPPDATA%\clio\clio-agent\.venv\Scripts\`, but the installer
   now writes to `%LOCALAPPDATA%\clio-agent\data\app\clio-agent\.venv\Scripts\`.
   Setting `CLIO_AGENT_GACT_BIN` worked around it.

Workarounds used: Git Bash and Go first on PATH when launching the app;
`CLIO_AGENT_GACT_BIN` set to the installed backend; the stale `data\app` folder
renamed.

## Problems

1. **Source-build is the first-run default for any build without a bundled
   runtime**, because the brand's `install.ref` is `develop`. Not verified:
   whether released *lite* installers inherit this (CI only asserts the brand is
   `managed/clio-agent`; no `install.ref` override found).
2. **Windows source mode depends on bash.** It should not: Windows is Windows,
   not Windows plus WSL or Git Bash. The TUI is a Go program and can be built
   with `go build` directly.
3. **Partial installs are not resumable** (guard refuses; recovery is manual).
4. **Launcher search paths are out of sync** with the installer prefix.
5. **Fast path is per-OS and per-variant**: runtime pack on Windows, resources
   on Linux deb/rpm, arm64-only on macOS. AppImage and Intel macOS get nothing.

## Proposal

- Make the in-app first-run install use **release mode** by default (empty or
  release `install.ref`); keep `CLIO_REF` an explicit developer opt-in. This
  fixes Windows, macOS and Linux at once and removes the git/go/bash needs.
- On Windows, do not call `bash` anywhere in `install.ps1`; build the TUI with
  `go build` (or drop source-mode TUI builds on Windows).
- Derive the launcher search path from the installer's prefix, or have the
  installer record it, instead of two hard-coded locations.
- Make the "installation already exists" guard offer resume/repair, or clean up
  after a failed run.
- Decide on one provisioning contract (a prebuilt runtime artifact consumed the
  same way on every OS) rather than a separate fast path per OS.

## Open questions

- Do released lite builds really run source-build on first run?
- Is there a plan for a bundled Intel macOS build and a bundled AppImage?
