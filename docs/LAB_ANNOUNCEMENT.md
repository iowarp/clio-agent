# Lab announcement template — copy/paste ready

Release date: 2026-04-27

---

## Slack / email body

> **CLIO v0.5.1 + GACT TUI v0.3.0 are out — give it a spin.**
>
> CLIO is the lab's scientific-data agent (HDF5 / Parquet inspection +
> analysis + visualization, plus arbitrary MCP tools). The new release
> ships with a full terminal UI and supports any LM provider you use
> daily — OpenAI / ChatGPT, Anthropic Claude, OpenRouter, Codex, or
> another OpenAI-compatible endpoint.
>
> **Five-minute install:**
>
> ```sh
> # Linux / macOS
> curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash
> clio
> ```
>
> ```powershell
> # Windows PowerShell
> irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
> clio
> ```
>
> The installer pulls `clio-agent` from PyPI and downloads the matching
> prebuilt `gact` binary from GitHub Releases. No local Go build or git
> checkout is needed for the default path. The LM-config modal pops on
> first connect; pick OpenAI / Anthropic / OpenRouter / Codex / LM
> Studio / Ollama / ALCF, paste a key if the provider needs one, and save.
>
> **Try one of these to see it in action:**
> - `Inspect /path/to/your.h5` — data expert + HDF5 tools
> - `What is the schema of /path/to/file.parquet?` — analysis expert
> - `validate parquet schema and statistics in parallel` — spawns
>   nanoagents (children appear in the sidebar)
> - `propose an edit to /path/to/file.py — switch to f-string` — diff
>   path with apply/reject
>
> **What's notable**
> - Every advertised capability (28/30 — only LSP + voice intentionally
>   off) is verified end-to-end. See
>   [CAPABILITIES_MATRIX.md](https://github.com/iowarp/clio-agent/blob/main/docs/CAPABILITIES_MATRIX.md).
> - Install + use any third-party MCP from npm/pypi (npx/uvx/raw stdio
>   or HTTP) via `POST /v1/mcp/servers`. Bundled fs/hdf5/parquet still
>   there.
> - Live cost meter in the footer; mid-session provider swap; full
>   audit trail for every destructive operation.
>
> **Setup help:**
> - `clio-agent/docs/SETUP.md` covers install modes and each provider
>   with troubleshooting.
> - `clio-agent/docs/LAB_USER_NOTES.md` has the install timing + rough
>   edges I hit during the rehearsal.
>
> **File bugs at**
> https://github.com/iowarp/clio-agent/issues — tag with `v0.5.1`.
>
> The TUI side is at https://github.com/iowarp/gact-tui/issues
> for UI-specific things (rendering, key bindings, etc.).

---

## Hero screenshots to attach

Each one is in `gact-tui/screenshots/` on the v0.3.0 tag:

1. `clio_doctor_caps_final.png` — capability scorecard (28/30 ✓ ✓ ✓)
2. `clio_real_turn.png` — real chat turn with cost meter visible
3. `clio_mcp_servers.png` — bundled + third-party MCP servers
4. `clio_subagent.png` — nanoagent children indented under parent
5. `clio_metrics.png` — backend metrics modal with live numbers

## If someone asks "is it stable?"

- Full integration suite: 16/16 strict in 95s, zero `xfail` markers.
- Release-mode install uses PyPI for CLIO and prebuilt GACT release
  assets; source-build mode remains available via `CLIO_REF` / `GACT_REF`.
- Older source-build rehearsal notes remain in `LAB_USER_NOTES.md` for
  historical debugging context.

## Contact

@jcernuda for setup help. PRs welcome — both repos use conventional
commits + branch off `tui-integration` (clio-agent) / `clio` (gact-tui).
