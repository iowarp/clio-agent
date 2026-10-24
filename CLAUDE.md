@AGENTS.md

## Cloud sessions

Cloud sessions install dependencies through the SessionStart hook (`scripts/cloud/session_start.sh`). Its log is `.cloud-run/session_start.log`.

- `bash scripts/cloud/stack.sh start|stop|restart|status|logs` runs the GACT server on 8787 and the gact-tui web UI on 5173. The model provider defaults to `claude_code`, which uses the Claude CLI that cloud sessions ship already signed in.
- `node scripts/cloud/ui_check.mjs [--send TEXT] [--steps FILE] [--width W --height H] [--label NAME]` drives the UI in headless Chromium. It prints screenshot paths, console errors and failed requests. Read the screenshots to check UI work yourself.
- Other providers, HPC systems and the Tauri desktop are not reachable from a cloud session.

## Current campaign

The plugins, agent SDK and flows change follows `docs/design/sdk/megachange-plan.md`. Read it before changing anything under `gact/` that touches blueprints, agents, prompts or workflows.
