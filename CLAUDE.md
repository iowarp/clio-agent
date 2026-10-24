@AGENTS.md

## Cloud sessions

Cloud sessions install dependencies through the SessionStart hook (`scripts/cloud/session_start.sh`). Its log is `.cloud-run/session_start.log`.

- `bash scripts/cloud/stack.sh start|stop|restart|status|logs` runs the GACT server on 8787 and the gact-tui web UI on 5173. The model provider defaults to `claude_code`, which uses the Claude CLI that cloud sessions ship already signed in.
- `node scripts/cloud/ui_check.mjs [--send TEXT] [--steps FILE] [--width W --height H] [--label NAME]` drives the UI in headless Chromium. It prints screenshot paths, console errors and failed requests. Read the screenshots to check UI work yourself.
- Other providers, HPC systems and the Tauri desktop are not reachable from a cloud session.

## What a live scenario proves

A prompt that names the tool, its arguments or the steps (for example "Start a workflow with run_workflow and approve_edits true… call workflow_add_step") verifies the mechanism only. It is not evidence of agent judgement: whether the agent picks that tool, flow or plan from a plain request, or whether a critic adds a step because the claim needs it. Record such a result as "mechanism verified with a tool-directed prompt; not evidence of agent judgement", never as a correctness pass. Only a plain request with no tool or steps named counts as evidence of the agent's choices, and so of the marketplace prompts and injected guidance that steer them.

## Current campaign

The plugins, agent SDK and flows change follows `docs/design/sdk/megachange-plan.md`. Read it before changing anything under `gact/` that touches blueprints, agents, prompts or workflows.
