# CLIO Site Capture Manifest

The public site (`site/`, served at clio.iowarp.ai) shows real product captures. This file lists the captures in use, what each one has to show, and the captures still to record. It is the checklist for anyone collecting screenshots for the site.

## Rules

- Record every capture from a real CLIO session on the current release. Do not use mockups, edited images, or QA fixtures.
- Show a real question, real data, and a finished result. Avoid empty states, settings-only views, provider setup screens, error states, and raw JSON.
- Use the CLIO-branded interface. Do not show GACT branding, debug rails, internal release names, or issue numbers.
- Save PNGs at 1246 × 720 or larger, at a consistent window size within a set. The build converts them to responsive WebP.
- Keep the file name when you replace a capture, so pages pick up the new image without code changes.

## In use on the overview (`site/src/assets/captures/`)

| File | Where | Must show |
|---|---|---|
| `earthscope-chart.png` | Hero | An interactive time-series chart from a real dataset, with the "N of M rows" sampling label visible |
| `earthscope-map.png` | 01 Ask | The agent pausing for the user to pick from an interactive map |
| `factorio-report.png` | 02 Delegate | Several expert child agents completed, with their report open beside the session |
| `agent-gantt.png` | 03 Watch | The Gantt timeline with the main agent, child agents, and tool calls |
| `provenance-evidence.png` | 04 Verify | The Evidence panel: files read, changed files, and artifacts |
| `deep-search.png` | 05 Continue | Session memory search returning matches from earlier sessions |

Known gaps in the current set (recorded on v0.9.2):

- `earthscope-map.png` and `earthscope-chart.png` use the light theme and the other captures use the dark theme. They also come from two different runs and stations.
- `deep-search.png` shows a qualification prompt, not a user question. Retake it with a natural follow-up question.

## Wanted

These open slots on the site are ordered by impact.

1. **Terminal interface.** The CLIO-branded terminal UI running a real analysis, for the Interfaces section and the command-line docs. The old `hero.png` (GACT-branded) was dropped for this reason.
2. **Desktop app window.** The native desktop window, with its title bar, showing a finished EarthScope or NDP result, for the Desktop card and the Install docs.
3. **One end-to-end EarthScope run in a single theme.** This replaces the hero, Ask, and tutorial captures with images from one session (see below).

## Tutorial captures

Each tutorial lists the captures it needs in a `Captures needed` or `Captures to refresh` note. A draft tutorial goes live when its captures exist.

### Your first analysis (published; refresh)

- The New session dialog with the **Agent blueprint** list open and EarthScope Skills selected.
- The map after a station is selected, with the confirm button visible.
- The time-series chart for the station chosen in the map capture.
- The Evidence tab for the same run, with the artifacts expanded to show content hashes.
- The artifact view with the **Versions** and **Lineage** tabs.

### Run CLIO fully offline with LM Studio (draft)

- Settings > **Models** with **LM Studio** selected and **Apply provider and model** visible.
- The LM Studio window with the local server running and a model loaded.
- The composer model control showing LM Studio and the model name.
- A finished answer to the workspace file question, with the tool calls visible.

### Install a blueprint from the marketplace (draft)

- The **Marketplaces** tab with the default marketplace expanded and an **Install** button visible.
- The **Installed** tab after installing, with the actions menu open.
- The **View details** dialog.
- The New session dialog with the **Agent blueprint** list open.
- The session header showing the blueprint name.
- The **Add marketplace** dialog.

### Give CLIO a new tool with an MCP server (draft)

- The **MCP tools** page with the **Connect MCP** button.
- The **Connect an MCP service** dialog filled in for a local command.
- The service detail dialog with its **Tools** tab.
- A session transcript showing the tool call and its result.
- The permission request for the tool call.

### Run the CLIO workspace on a server with Docker (draft)

- A terminal showing `docker compose up clio-web` with the container running.
- The web interface open in a browser on another computer, with the address bar visible.
- The `host_not_allowed` error, and the same page working after `CLIO_GACT_ALLOWED_HOSTS` is set.
- The model settings page showing the provider taken from the environment variables.

## Adding captures

See `site/README.md` under "Add a tutorial" and "Refresh product captures".
