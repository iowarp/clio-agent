# Site docs audit (v0.9.6.1)

Scope: every page under `site/src/content/docs/` except the home page. Each page
is checked against three things:

- the UI that v0.9.6.1 ships;
- current `develop`;
- the style rules in `site/WRITING.md`.

## Baseline: which UI the docs must describe

v0.9.6.1 pins gact-tui `c3917b62` (v0.12.1). The GitHub compare API shows that
commit comes after the gact-tui #1138–#1142 merges. So the shipped UI already
includes:

- the right workbench icon strip (Files, Sources, Artifacts, Activity, Agent,
  Terminal, Browser, then Maximize canvas), which replaced the `+` tab button;
- lists without header rows;
- the New page tab;
- the in-app browser;
- comment pins.

Current gact-tui `develop` (47f5474fe) matches the shipped UI for everything
these docs describe.

The site's own live widget build (`.github/workflows/pages.yml`) still checks
out gact-tui `4983d3bd`. That is older than the shipped UI, so live components
on the site can drift from what users see. See the proposal.

## UI that no longer exists, but the docs still describe

Checked by text search across all pages.

| Removed UI | What replaced it |
|---|---|
| **Observability** tab (Evidence, Timeline, Gantt, Context) | The **Activity** canvas tab (timeline grouped *Now*, *This turn*, *Earlier*), plus artifact **Lineage** |
| **Work** tab, *Work view*, *Open Work view* | Running work, todos and goals in the conversation rail. Child agents open as their own conversations. |
| Compact Data and Work summaries, *Show completed*, *Open full details* | Rail sections and the Activity timeline |
| The `+` new-tab button | The right icon strip and the New page tab |
| "side panel" | The **canvas** (header toggle *Open workspace canvas*) and its tabs |
| *Create or import* | *Save as a setup…* in the Agent tab, and marketplace install |
| Runs → *All states* | Activity timeline filters |

Pages with hits, with the count for each stale term:

- **workspace-panels**:
  - Observability ×6 and Gantt ×6;
  - Work view and Work tab ×4;
  - Show completed and Open full details.
- **following-work**: Show completed ×4, side panel ×3, Work view, Gantt.
- **branch-and-rewind**: Gantt ×5, Open full details.
- **evidence**: Gantt ×5, Context tab, Open full details.
- **browser**: side panel ×7.
- **tasks pages**:
  - pages: tasks/index, tasks/subagents, tasks/tools-and-sources,
    tasks/session-coordination and tasks/recorded-conversations;
  - terms: Open Work, Show completed, Observability, All states.
- **sessions** and **agents-and-plugins**: Create or import, side panel, Gantt.
- **dashboards**, **flows**, **scripts**, **live-sources** and **sdk/available**:
  side panel.
- **memory**, **permissions** and **scripts**: Observability.
- **examples/ground-motion**: Gantt, Open full details.

## Internal names and obsolete gates (WRITING.md violations)

**Obsolete version gates:**

- "Requires v0.9.5-beta.8" appears in `files/index` and `examples/index`.
- "Development preview · UI f3bac76f" appears in `questions` (×3) and in
  `extensions`.

**`CLIO_GACT_*` variables in user pages** appear in:

- `privacy` (×5);
- `configuration`;
- `server-setup`;
- `troubleshooting` (`CLIO_GACT_ALLOWED_HOSTS`);
- `tutorials/guides/docker-on-a-server`.

**Tool and runtime internals:**

- `wait_tasks`, handles and "owner settlement": `tasks/*`,
  `background-work-reference` and `sdk/available`.
- `gact_*` tool names: `memory`.
- `knowledge_search`: `files/knowledge` and `sdk/available`.
- "fleet": `background-work-reference`, `models-and-usage`, `sdk/*` and
  `agents-and-plugins`.
- `clio-core`: `uninstall` (×4) and `live-sources`.

**Repeated TUI warnings** appear in seven pages: `cli`, `install`,
`install-with-ai`, `server-setup`, `contributing`, `privacy` and
`docker-on-a-server`. Say it once, on the versions or install page.

**Wrong default port (verified).** `cli.mdx` says `clio-agent serve` defaults to
port 8100 (lines 91 and 103). It actually defaults to `gact.port`, which is
17800. Evidence: `src/clio_agent/server_port.py:28` and `gact/app.py:1736`.

**Contradiction.** `install` and `versions` disagree on whether beta updates are
offered.

**Not portable.** `uninstall` uses `/tmp` paths, which don't exist on Windows.

## Per-page verdicts

| Code | Meaning |
|---|---|
| **R** | Rewrite: describes removed UI, or reads as internal or QA documentation |
| **L** | Light revision: wording, a few terms, a screenshot check |
| **OK** | Current; needs a style pass only |
| **M** | Move or merge (see the proposal) |

### Get started

| Page | Verdict | Notes |
|---|---|---|
| index (docs home) | L | A good entry. Point it at the new "Your workspace" tour. |
| install | L | Collapse the TUI warning. Fix the beta-updates contradiction with versions. |
| install-with-ai | L | TUI warning ×2. |
| connect-a-model | OK | |
| versions | L | Make it the single home for the TUI notice and update channels. |
| workspace-panels | **R** | Entirely the old UI: Observability, Work tab, compact summaries, `+`. All 5 captures are stale. **This is the showcase page.** |
| uninstall | L | Drop `/tmp` and `clio-core`. Give per-OS paths. |

### Bring your data

| Page | Verdict | Notes |
|---|---|---|
| working-with-files | L | Overlaps files/index. Keep one. |
| files/index | L | Remove the v0.9.5-beta.8 gate. |
| files/attach, download-or-link, manage, references | OK | Check screenshots against the strip's Files tab. |
| files/connect | R | Describes a connect flow that moved to the Sources strip button. |
| files/knowledge | L | Remove `knowledge_search`. |
| files/ndp, ndp-streams | OK | Captures are current. |
| live-sources | R | Uses "side panel" and `clio-core`. Puts stream internals in a user page. |

### Work with CLIO

| Page | Verdict | Notes |
|---|---|---|
| sessions | L | Remove "Create or import", "side panel" and Gantt. |
| questions | L | Remove the "Development preview" banners. The question dock is current: 1/3 pager, numbered options, write-in. |
| commands | L | Move the plugin-translation section to the SDK pages. |
| following-work | **R** | Rewrite around the rail and the Activity tab. |
| injections | R | Move the user-facing half to "Follow work in progress", and the mechanism to reference. |
| branch-and-rewind | R | Gantt ×5. The branch flow itself is still valid. |
| best-of-n | OK | |
| subagents + tasks/subagents | M | Merge. Child agents open as their own conversations. |
| tasks/index, observe-and-wait, tools-and-sources, session-coordination, messages | **R/M** | Read as agent, API or QA notes: `wait_tasks`, handles, "native-replacement qualification", Codex/Luna test reports. Split: user parts go to "Follow work in progress", API to Technical reference. |
| tasks/recorded-conversations | M | Move to Worked examples. |
| long-running-work + background-work-reference | R/M | Merge the user halves. Move `fleet` and `wait_tasks` to reference. |
| browser | L | Change "side panel" ×7 to the canvas Browser tab. Move browser permissions to permissions. |
| memory | L | Remove Observability and `gact_*`. |
| permissions | L | Remove Observability. Take over the browser-permissions section. |
| scripts | L | Remove Observability and "side panel". |
| models-and-usage | L | Rename to "Choose a model and view usage". Remove "fleet". |

### Review and share results

| Page | Verdict | Notes |
|---|---|---|
| evidence | **R** | Gantt, Context tab. Now covered by Activity plus artifact Preview/Versions/Lineage/Export. |
| comments | L | Pins are current. Check the captures against #1141. |
| dashboards | L | One "side panel". |
| papers | L | 17 images, mostly current. Trim. |
| exports | OK | Add Office files: Export to PDF beside Download. |
| science-log | OK | |
| widgets (user page) | L | Keep the user page. Move components and hooks to SDK. |

### Find and revisit work

| Page | Verdict | Notes |
|---|---|---|
| search | OK | Screenshots current. |
| long-term-memory | L | Rename to "Search past conversations". |

### Worked examples

| Page | Verdict | Notes |
|---|---|---|
| examples/index | L | Remove the v0.9.5-beta.8 gate. Reorder. |
| check-a-dataset | R | "Open Data" no longer exists. |
| ground-motion | R | Gantt, Open full details. |
| reports-and-slides, research-notes | L | |
| branched-study | L | 4 captures to check against the current strip. |
| ndp-science, ndp-earthscope | M | Move to a case-study hub, separate from tutorials. |

### Customize CLIO

| Page | Verdict | Notes |
|---|---|---|
| agents-and-plugins | L | Remove "Create or import", "side panel" and "fleet". Point to the Agent tab. |
| marketplaces/index, manage | OK | |
| mcp-servers | OK | |
| flows | L | "side panel". |
| hooks | OK | |
| extensions | R | "Development preview" banner. Remove it, or label as preview in one place. |
| configuration | L | Move `CLIO_GACT_*` to a reference table. |

### Build with the SDK

| Page | Verdict | Notes |
|---|---|---|
| sdk/index, agents, plugins, flows, workflows, build-a-plugin | L | Remove "fleet". |
| sdk/available | R | Presents internal tool names (`wait_tasks`, `knowledge_search`, fleet) as user vocabulary. |
| sdk/reference | R | Out of date with the plugin SDK (megachange plan). |
| widgets/* (component catalog) | M | Generated pages. Move under SDK → UI components. |

### Help, privacy, reference and tutorials

| Page | Verdict | Notes |
|---|---|---|
| troubleshooting | R | Work view, `CLIO_GACT_ALLOWED_HOSTS`. Reorganize by symptom. |
| privacy | L | `CLIO_GACT_*` ×5, TUI. |
| cli | L | **Wrong port** (see above). TUI ×4. |
| server-setup | L | Rename to "Run CLIO on a server". TUI ×4. |
| contributing | OK | TUI ×2. |
| managed-infrastructure | OK | |
| tutorials/guides/remote-deployment | R | 15 PNGs of the old start screen and service selector. |
| tutorials/guides/docker-on-a-server | L | `CLIO_GACT_*`, TUI. |
| tutorials/guides/local-model, add-an-mcp-server | R | Draft quality. Finish or remove. |

## Visual inventory

**Pages with no images.** About 30 pages have no image at all, including:

- the whole `tasks/*` group;
- the peers of following-work;
- cli, install, configuration and troubleshooting.

Text-only pages for UI features are the main cause of the "limited visual
elements" complaint.

**Captures that show removed UI:**

- workspace-panels: all 5 (`compactWork`, `evidence`, `gantt`, `work`,
  `preview`);
- the Gantt captures in evidence and branch-and-rewind;
- following-work's Work view captures;
- remote-deployment's 15 old start-screen PNGs.

**Captures to keep.** The RC1 captures refreshed in #2068 (home, search,
papers, comments, NDP) still match the UI, and the user likes them.

**Live UI and clips.** Today no page renders a live UI component, except the
generated widget catalog pages. No page uses a click clip, although
`WidgetRecording.astro` and the `site/video/` pipeline both exist.

## Writing and structure problems (cross-cutting)

- **Mixed groups.** Several sidebar groups mix user tasks with mechanism and QA
  notes. "Tasks and coordination" is the worst.
- **Duplicates.** The same feature is documented in two or three places:
  - subagents (2 pages);
  - long-running work (2 pages);
  - the files introduction (2 pages);
  - browser permissions, in both browser *and* permissions.
- **Mixed examples.** Worked examples mix step-by-step tutorials with NDP case
  studies.
- **Broken directions.** Pages locate features in panels that no longer exist,
  so even correct steps fail at "open the X tab".

## Claims still to verify

Check these against the running UI before rewriting:

1. *files/connect*: is "Connect a source" reached only from the Sources strip
   button, or also from the composer?
2. *check-a-dataset*: what replaces "Open Data": the Files canvas tab or the
   Sources tab?
3. *comments*: do the pin captures predate #1141?
