# CLIO Site Shot List

This is the list of product screenshots for the public site (`site/`, served at clio.iowarp.ai). Each entry says where the image goes, what the app must show, how to get there, and the file to save it as. The site leads with the desktop app, so every shot is taken in the desktop app unless the entry says otherwise. The terminal interface needs no shots.

Save each file at the path given. If a file with that name already exists, replace it; its page picks up the new image with no code change. Entries marked **new slot** need a small code change to place the image, so mention them in the pull request that adds them.

## Current homepage captures, 2026-10-06

The homepage now leads with a full-width, tabbed OPAL showcase below the headline.
These entries explicitly use **browser captures**, in light mode at 1440 × 1000
and 1× scale. They show a development review, not a published Desktop release.
The source settings detail uses a 1000 × 620 browser viewport so its account
rows remain readable in the guide.

| File | Slot | Recorded behavior |
| --- | --- | --- |
| `site/src/assets/captures/opal-figure-viewer.png` | Figures | A complete six-panel OPAL plant-area figure, with dates, units and IQR bands. Treatment doses remain unconfirmed. |
| `site/src/assets/captures/opal-report-review.png` | Documents | Review of an existing three-page Word report and eleven-slide PowerPoint deck, with a saved PDF preview beside the conversation. |
| `site/src/assets/captures/opal-review-evidence.png` | Evidence | The same document review's tool calls, source documents and artifacts. |
| `site/public/media/source-accounts.png` | Connect sources guide | Global account sign-in, separate from workspace data attachment. All accounts are signed out. |

See `site/src/assets/captures/README.md` for source commits and capture metadata,
and `docs/SITE_ERGONOMICS_REVIEW.md` for rendered review and test evidence.
The EarthScope chart is no longer in the hero. The five older workflow examples
are retained in expandable rows, with their original session captions. The
numbered shot list below is an older refresh plan, not a description of one
continuous session currently shown by the homepage.

## How to shoot

| Setting | Value |
|---|---|
| App | The CLIO desktop app on the current release, bundled build |
| Window | 1440 × 900, not maximized, title bar visible for shots marked "window"; content area only otherwise |
| Scale | 2× (Retina or 200% display scaling); save as PNG |
| Theme | Dark, for every shot, so the set matches the site |
| Model | A capable hosted model; the model chip in the composer will be visible, so use one you are happy to show |
| Workspace and session names | Plain names a user would choose, such as "Ground motion" or "Fire season". Never release or test names like "Release v0.9.2 qualification" or "candidate". |
| Status bar | Close or hide anything that shows "Unavailable", errors, or debug counters |

Avoid in every shot:

- error states, retries, and provider setup failures
- raw JSON or debug panels
- personal data such as paths with your user name, email addresses, or API keys
- notification badges and empty states, unless the entry asks for them

## One story, one session

Shots 1 through 5 come from **one** session, so the overview tells a single continuous story:

- **Blueprint:** EarthScope (the multi-expert one, `earthscope-gnss-region`)
- **Workspace:** "Ground motion"
- **Session:** "Palm Springs stations"
- **First prompt:**
  > Find the five EarthScope GNSS stations nearest Palm Springs, California, show them on a map, and wait for me to choose one.
- **Then:** choose one station on the map and confirm.
- **Follow-up:**
  > Plot the east, north, and up position of that station over its full record and tell me how much to trust the data.

Let the run finish before you take shots 2 through 5.

## Overview page

### 1. Hero: the chart

- **File:** `site/src/assets/captures/earthscope-chart.png`
- **Where:** first screen of the overview, beside the headline
- **Show:**
  - the session with the interactive time-series chart fully in view, east, north, and up series
  - the "N evenly sampled rows from M total" label visible
  - the hover tooltip open on one point
  - the left sidebar with the workspace and session names
- **Frame:** content area, no title bar

### 2. Ask: choosing on the map

- **File:** `site/src/assets/captures/earthscope-map.png`
- **Where:** overview section 01 Ask, and the first tutorial
- **Show:**
  - the "response needed" question with the interactive map
  - five labeled stations, with one selected and highlighted in the location list
  - the confirm control visible
- **Frame:** content area

### 3. Delegate: experts in parallel

- **File:** `site/src/assets/captures/factorio-report.png` (keep the name even though the content changes)
- **Where:** overview section 02 Delegate
- **Show:**
  - the transcript where the expert child agents have completed, each with its completion time and green check
  - the resulting report artifact open in the side panel, with the Preview, Versions, and Lineage tabs visible
- **Frame:** content area

### 4. Watch: the timeline

- **File:** `site/src/assets/captures/agent-gantt.png`
- **Where:** overview section 03 Watch
- **Show:**
  - the observability dock on the Gantt tab
  - the main agent, at least two child agents, and their tool calls as bars over time
  - the transcript on the left showing the final answer
- **Frame:** content area

### 5. Verify: the evidence

- **File:** `site/src/assets/captures/provenance-evidence.png`
- **Where:** overview section 04 Verify, and the first tutorial
- **Show:**
  - the observability dock on the Evidence tab, listing "Read by agent" files, Changed files, and Artifacts
  - one artifact expanded so its content hash shows
- **Frame:** content area

### 6. Continue: earlier work

- **File:** `site/src/assets/captures/deep-search.png`
- **Where:** overview section 05 Continue
- **How:**
  - Start a **new** session in the same workspace.
  - Ask: "Last week I looked at a GNSS station near Palm Springs. Which one was it, and what did we conclude?"
- **Show:**
  - the memory search tool call expanded, with its matches from the earlier session
  - the answer that names the station
- **Frame:** content area

### 7. Desktop app window (new slot)

- **File:** `site/src/assets/captures/desktop-window.png`
- **Where:**
  - the Desktop app card in the overview's Interfaces section
  - the top of the Install page
- **Show:** the whole desktop window, title bar included, on a plain desktop background, with the finished session from shots 1 through 5 visible.
- **Frame:** window

## Docs pages (all new slots)

### 8. First launch: choose a model

- **File:** `site/src/assets/docs/first-launch-provider.png`
- **Where:** Connect a model, top
- **Show:** the model provider dialog that appears the first time the desktop app starts, with the provider list open.
- **Frame:** window

### 9. Models settings

- **File:** `site/src/assets/docs/settings-models.png`
- **Where:** Connect a model, "Change the model later"
- **Show:** Settings, Models page, with a provider selected and **Apply provider and model** visible.

### 10. New session

- **File:** `site/src/assets/docs/new-session.png`
- **Where:** Sessions and modes
- **Show:** the New session dialog with the **Agent blueprint** list open and EarthScope highlighted.

### 11. Composer controls

- **File:** `site/src/assets/docs/composer-controls.png`
- **Where:** Sessions and modes, "Choose how the agent works"
- **Show:** the composer with the Execution mode menu open, showing Execute, Plan, and Deep research.
- **Crop:** the composer area only

### 12. Permission prompt

- **File:** `site/src/assets/docs/permission-prompt.png`
- **Where:** Permissions and sandbox
- **Show:** a pending permission request for a file write, with Allow once, Deny, Allow for session, and Allow for workspace visible.
- **How:** set the confirmation policy to Ask first, then ask the agent to save a summary to a file.

### 13. Diff review

- **File:** `site/src/assets/docs/diff-review.png`
- **Where:** Permissions and sandbox, and Memory and evidence
- **Show:** a proposed file change open in the diff view, with **Apply change** and **Reject change** visible.

### 14. Marketplace

- **File:** `site/src/assets/docs/marketplace.png`
- **Where:** Agent blueprints
- **Show:** Settings, **Marketplaces & blueprints**, on the Marketplaces tab, with the default marketplace expanded and several blueprints listed, one with an **Install** button.

### 15. MCP tools

- **File:** `site/src/assets/docs/mcp-tools.png`
- **Where:** Tools and MCP servers
- **Show:** the **MCP tools** settings page with one connected server and the **Connect MCP** button visible.

### 16. Timeline and Context tabs

- **Files:**
  - `site/src/assets/docs/observability-timeline.png`
  - `site/src/assets/docs/observability-context.png`
- **Where:** Memory and evidence
- **Show:** the observability dock on the Timeline tab, then the same session on the Context tab.

## Tutorials

Each tutorial lists its shots in a `Captures needed` or `Captures to refresh` note at the end of the page. Save them in `site/src/assets/tutorials/<tutorial-slug>/`. A draft tutorial goes live once its shots exist.

### Your first analysis (published; refresh)

Use the same session as shots 1 through 5.

- The New session dialog with the **Agent blueprint** list open and EarthScope selected.
- The map after you choose a station, with the confirm control visible.
- The time-series chart for that same station.
- The Evidence tab for the same run, with the artifacts expanded to show content hashes.
- The artifact view with the **Versions** and **Lineage** tabs.

### Run CLIO fully offline with LM Studio (draft)

- Settings > **Models** with **LM Studio** selected and **Apply provider and model** visible.
- The LM Studio window with the local server running and a model loaded.
- The composer model chip showing LM Studio and the model name.
- A finished answer to the workspace-files question, with the tool calls visible.

### Install a blueprint from the marketplace (draft)

- The **Marketplaces** tab with the default marketplace expanded and an **Install** button visible.
- The **Installed** tab after the install, with the actions menu open.
- The **View details** dialog.
- The New session dialog with the **Agent blueprint** list open.
- The session header showing the blueprint name.
- The **Add marketplace** dialog.

### Give CLIO a new tool with an MCP server (draft)

- The **MCP tools** page with the **Connect MCP** button.
- The **Connect an MCP service** dialog filled in for a local command.
- The service detail dialog on its **Tools** tab.
- A session transcript showing the tool call and its result.
- The permission request for the tool call.

### Run the CLIO workspace on a server with Docker (draft)

This is the one set taken outside the desktop app.

- A terminal showing `docker compose up clio-web` with the container running.
- The web interface open in a browser on another computer, with the address bar visible.
- The `host_not_allowed` error, and the same page working after `CLIO_GACT_ALLOWED_HOSTS` is set.
- The Models settings page showing the provider taken from the environment variables.

## After you add shots

See `site/README.md`, sections "Add a tutorial" and "Refresh product captures".
