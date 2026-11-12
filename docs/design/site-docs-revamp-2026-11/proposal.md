# Site docs revamp: proposal

Companion to [audit.md](audit.md). Scope: `site/src/content/docs/` (docs,
tutorials and examples, SDK, widgets), not the home page.

## Goals

1. **Every page describes v0.9.6.1.** No removed panels, version gates, internal
   variable names or tool names.
2. **Each feature lives in one place.** User tasks go in the guides; mechanism,
   API and environment variables go in Technical reference.
3. **Show the UI.** Each UI page uses at least one of: a current screenshot, a
   live-rendered production component, or a short click clip.

## New navigation

Group names are tasks. Pages marked *new* or *merged* come from the audit.

### Get started

| Page | Change |
|---|---|
| Install | Absorbs install-with-ai as a tab |
| Connect a model | |
| **Your workspace** | Rewrite of workspace-panels. *The showcase page.* |
| Your first analysis | check-a-dataset rewritten as the first tutorial |
| Versions and updates | The one home for the TUI notice and update channels |

### Bring your data

| Page | Change |
|---|---|
| Files in CLIO | Merges working-with-files and files/index |
| Attach, link and download | Merges attach and download-or-link |
| Connect a source | files/connect, now via the Sources strip button |
| Live sources | |
| Knowledge folders | |
| NDP datasets and streams | Merges ndp and ndp-streams |

### Work with CLIO

| Page | Change |
|---|---|
| Conversations | sessions |
| Answer questions | questions, without the preview banners |
| **Follow work in progress** | Merges following-work, injections (user half) and the user parts of tasks/index and observe-and-wait. Covers the rail plus Activity. |
| Long-running and background work | Merges long-running-work with the user half of background-work-reference |
| Subagents | Merges subagents and tasks/subagents |
| Branch, rewind and compare | Merges branch-and-rewind and best-of-n |
| Use the browser | |
| Run scripts and the terminal | |
| Commands | The plugin-translation section moves to the SDK pages |
| Choose a model and view usage | Renamed from models-and-usage |
| Permissions and protected execution | Takes over the browser-permissions section |

### Review and share results

| Page | Change |
|---|---|
| Artifacts and lineage | Rewrite of evidence: Preview, Versions, Lineage, Export |
| Dashboards and widgets | User page only |
| Comments and pins | |
| Papers | |
| Reports, slides and exports | Merges exports with the Office-to-PDF note |
| Science log | |

### Find and revisit work

| Page | Change |
|---|---|
| Search | |
| Search past conversations | Renamed from long-term-memory, absorbs memory |

### Tutorials

Ordered by difficulty:

1. Check a small dataset
2. Ground motion
3. Reports and slides
4. Branched study
5. Research notes
6. Recorded conversations (moved from tasks/)

### Case studies

NDP science and NDP EarthScope, in their own hub, separate from the tutorials.

### Customize CLIO

- Agents, setups and plugins: points to the Agent tab and *Save as a setup…*
- Marketplaces
- MCP servers
- Flows
- Hooks
- Configuration: settings only. Environment variables move to reference.
- Extensions: one "preview" label, or removed until shipped.

### Build with the SDK

| Page | Change |
|---|---|
| Overview, Agents, Plugins, Flows, Workflows, Build a plugin | |
| UI components | The generated widget catalog moves here, from `widgets/*` |
| Coordination API | Moved from tasks/: messages, session-coordination, `wait_tasks`, handles |
| Reference | |

### Run CLIO on a server

- Run CLIO on a server (renamed from server-setup)
- Docker on a server
- Remote deployment (recaptured)
- Managed infrastructure

### Help and reference

- Troubleshooting, by symptom
- CLI (port fixed)
- Environment variables (new; collects every `CLIO_*` and `CLIO_GACT_*`)
- Privacy
- Uninstall
- Contributing

Tutorial drafts (local-model, add-an-mcp-server): finish them, or remove them
from the nav.

**Redirects.** Every moved page keeps its old URL through `redirects` in
`astro.config.mjs`, so links in releases and issues keep working.

## Visual plan

Three kinds of visuals, picked per page by what the reader needs.

### 1. Screenshots: kept, and the default

Keep the RC1 captures from #2068, which the user likes. Retire the stale ones
listed in the audit, and recapture them from the shipped UI, dark and light, at
the existing sizes, with `captureImage()`/`Capture`.

### 2. Live production components: for things you interact with

The UI is React. `site/widget-engine/widget-docs.tsx` already mounts real
gact-tui components inside docs pages through `WidgetLive.astro`. Each new
`data-clio-widget-live` mode renders a production component with fixture data:

| Mode | Production component | Used on |
|---|---|---|
| `question` (showcase) | `QuestionResponse`: question dock with pager, numbered options, write-in | Your workspace; Answer questions |
| `activity` | `ClioActivityTimelineList` with fixture messages | Follow work in progress |
| `artifact` | artifact Preview/Versions/Lineage viewer | Artifacts and lineage |
| `archive` | `offline-review.tsx` replaying an exported conversation | Tutorials (a real conversation you can scroll) |

These components are the shipped code, not mock-ups. They follow the site
theme, and they are keyboard-accessible.

**Required change.** Bump the gact-tui ref in `.github/workflows/pages.yml` from
`4983d3bd` to the release pin `c3917b62` (v0.12.1). Otherwise the live
components lag the shipped UI. Long term, read the ref from the release pin
rather than hard-coding it.

### 3. Short click clips: for flows that cross panels

Clips are 4–10 s, muted, looped MP4s with a poster and VTT captions, played by
`WidgetRecording.astro`. They're recorded with the existing `site/video/`
Playwright pipeline:

- `pointer-guide.mjs` draws the cursor and click ripple;
- `render-ffmpeg.mjs` encodes the video.

Each clip has a still poster, so readers who prefer reduced motion and readers
on slow connections still see the step.

Candidate clips:

- open the canvas and tour the strip (showcase);
- answer a 3-question dock;
- branch from a message;
- pin a comment;
- export an Office file to PDF;
- connect a source.

Budget: under 600 KB per clip, and at most two per page.

## Delivery

1. **This PR (for review).** Contains:
   - audit and proposal;
   - the `question` live mode;
   - one clip;
   - the showcase page "Your workspace";
   - the pages.yml gact-tui bump.
2. **After approval**, one PR per sidebar group, in this order:
   1. Get started
   2. Work with CLIO
   3. Review and share
   4. Data
   5. Tutorials and case studies
   6. Customize and SDK
   7. Server, Help and reference

   Each PR also ships its redirects, recaptures and clips, plus a local build
   with screenshots.

## Open decisions for the user

1. Extensions: label as preview, or hide until shipped?
2. Move the widget catalog under SDK? (It changes `/docs/widgets/*` URLs;
   redirects keep the old links working.)
3. Finish the two draft tutorials, or remove them?
