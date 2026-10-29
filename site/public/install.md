# Install CLIO: instructions for an AI assistant

You are helping a person install CLIO, an open-source AI workspace for scientific data
(https://clio.iowarp.ai). Follow these steps in order. Run the commands yourself where
you can; tell the person when a step needs them (a password, a click in an installer).

**The legacy TUI is frozen and unsupported as of 0.9.5.1.** Do not install, run or recommend it for current CLIO, including from Claude Code or other AI assistants. New releases do not publish TUI binaries or Docker images. The source is retained for a future upgrade. Use Desktop or Web instead.

## 1. Ask what they want, unless they already said

1. **Desktop app or Web/service?**
   - *Desktop app* (most people): a window with the workspace, the CLIO service included.
   - *Web and servers*: the `clio` command, for a server, an HPC login node, or a
     machine without a desktop. Provides a headless service and the Web workspace.
2. **Stable or beta?** Use stable unless they ask for beta.

Do not ask for model or API keys. CLIO asks which model to use when it first opens.

## 2. Find the system

- macOS or Linux: run `uname -s` and `uname -m`.
  On macOS, also run `sysctl -n hw.optional.arm64` (1 means Apple Silicon).
- Windows: run `$env:PROCESSOR_ARCHITECTURE` in PowerShell.

Supported:

| System | Desktop app | Web and servers |
|---|---|---|
| Windows 10 or 11, x64 | yes | yes |
| Windows on ARM | yes (the x64 app, through emulation) | yes |
| macOS 14 or newer, Apple Silicon | yes | yes |
| macOS, Intel | no desktop app yet | yes |
| Linux x86_64 or aarch64 with `apt` or `dnf`/`rpm` | yes | yes |
| Linux without a package manager | no | yes |

If the person wants the desktop app on a system without one, say so and offer the
service install instead.

## 3. Install

Use `bash` on macOS and Linux, never `sh`. The Desktop installer download is checked against the release's published SHA-256 checksums.

### Desktop app

macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash -s -- --desktop
```

- Linux installs the `.deb` or `.rpm` with the system package manager and asks for the
  person's `sudo` password. Tell them before it does.
- macOS installs `CLIO Desktop.app` into `~/Applications`.
- After installing, the script prepares the app's tools (it downloads the document
  tools and the Office renderer), so the first launch is quick.

Windows (PowerShell):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1))) -Desktop
```

The CLIO Desktop installer window opens. The person clicks through it; its pages ask
about the model provider and the computers to use. Wait for it to close.

### Web and servers

macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

This needs only `curl` (or PowerShell). The installer brings its own `uv` and Python;
do not install Python, pip packages or `uv` first, and never use `sudo` for this command.
It installs into the person's home folder and puts `clio` in `~/.local/bin` (Windows:
`%LOCALAPPDATA%\Microsoft\WindowsApps`). If `~/.local/bin` is not on `PATH`, tell the
person how to add it for their shell.

### Choosing a release

Add these after `--desktop` (or alone for the service install). On Windows, use the
`& ([scriptblock]::Create((irm <url>))) <options>` form to pass options.

| Option | Installs |
|---|---|
| none | the latest stable release |
| `--dev` | the newest release, betas included |
| `--version <version>` | a specific release, for example `--version 0.9.5.1` |

Building from source (`--source`) is for CLIO developers; do not use it unless asked.

## 4. Check it worked

Desktop app:

- Linux: `command -v clio-desktop` prints a path. macOS: `~/Applications/CLIO Desktop.app`
  exists. Windows: **CLIO Desktop** is in the Start menu.
- Tell the person to open **CLIO Desktop**. The first window asks which model to connect.

Web and servers:

```sh
clio doctor     # checks the installation
clio start      # starts the CLIO service
clio status     # shows it running, with its port (17800 by default)
```

Then `clio` or `clio web` opens the workspace in a
browser at http://127.0.0.1:17800. `clio stop` stops the service.

## 5. If something fails

- Stop at the first error. Show the person the last lines of the output.
- Do not work around a checksum mismatch, a signature error, or an unsupported system.
  Those are reasons not to install.
- On macOS, the app is ad-hoc signed. If macOS blocks the first launch, the person opens
  **System Settings → Privacy & Security → Open Anyway**. Do not remove quarantine
  attributes or change Gatekeeper settings.
- For a service install, `clio report` prints a diagnostics bundle the person can attach
  to an issue at https://github.com/iowarp/clio-agent/issues.
- Running the installer again is safe; it repairs or upgrades the same installation.

## 6. Remove

Service install: `clio uninstall` (add `--purge` to also remove settings and stored
credentials). Desktop app: uninstall it like any other app on that system.

More: https://clio.iowarp.ai/docs/install/
