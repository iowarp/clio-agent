# CLIO Install Page Image Manifest

This is the curated screenshot set for a clean CLIO install/download page.
These are product images, not QA evidence dumps. Only use screenshots that show
the current CLIO visual system, real conversation context, and concrete outputs
such as generated plots, rendered Markdown, image previews, or diffs.

Do not use old terminal screenshots, debug rails, error traces, provider setup
screens, or fixture screenshots that expose bugs already fixed in the current
interfaces.

## Current Public Set

All paths below are relative to the `clio-agent` repository.

### 1. Hero: Live EarthScope/NDP Workflow With Plot Output

- Source: `external/gact-tui/apps/web/screenshots/audit/ndp-earthscope-live-final.png`
- Use for: first viewport hero image.
- Why: Current CLIO web shell, real scientific workflow, visible session
  conversation, generated GNSS plot artifact, and file preview rail.
- Caveat: This is the best current hero, but a fresher retake with a denser
  final answer and the plot more central would be better.

### 2. Artifact Preview: Scientific Plot Inspection

- Source: `external/gact-tui/apps/web/screenshots/audit/ndp-earthscope-live-artifact-preview.png`
- Use for: carousel slide immediately after the hero.
- Why: Same visual theme as the hero and still anchored to the EarthScope/NDP
  workflow. Shows a generated plot file in context.

### 3. Markdown Output: Rendered Report Preview

- Source: `external/gact-tui/apps/web/screenshots/audit/overnight-real-markdown-preview.png`
- Use for: "reports and evidence" feature tile.
- Why: Shows rendered Markdown with a table and checklist-style content rather
  than a raw text block.

### 4. Image Output: Workspace Image Preview

- Source: `external/gact-tui/apps/web/screenshots/audit/overnight-real-image-preview.png`
- Use for: "generated artifacts" feature tile.
- Why: Demonstrates binary/image preview support in the same current web shell.

### 5. Diff Output: Review Proposed Edits

- Source: `external/gact-tui/apps/web/screenshots/audit/overnight-real-file-editor-diff.png`
- Use for: "review changes before applying" feature tile.
- Why: Shows a meaningful code diff pane, not just a generic file list.

### 6. Streaming Conversation: Live Response State

- Source: `external/gact-tui/apps/web/screenshots/audit/overnight-real-streaming-final.png`
- Use for: secondary carousel slide.
- Why: Shows live conversation output in the current shell.
- Caveat: Use only after the richer artifact/Markdown/diff images. It is
  useful but less visually interesting.

### 7. Desktop Shell: Native App Framing

- Source: `external/gact-tui/apps/web/screenshots/audit/desktop-linux-xvfb-chat-clio-18190-attached-1440.png`
- Use for: desktop download card or native app section.
- Why: Current CLIO-branded native shell.
- Caveat: It is an empty/starting state. Do not use it as the hero. Retake with
  the EarthScope/NDP workflow or a generated artifact before using prominently.

## Do Not Use

These are valuable QA fixtures, but they are not public product imagery:

- `external/gact-tui/screenshots/*`
  - Mostly older TUI generations with stale visual language or old bugs.
- `external/gact-tui/visual_loop/screenshots/semantic_*.png`
  - Regression fixtures. Many are intentionally narrow, debug-heavy, or focused
    on a single control state.
- `external/gact-tui/visual_loop/screenshots/tui-live-streaming-fixed-final.png`
  - Current enough for QA, but GACT-branded and generic. Retake with CLIO brand
    and a real workflow before public use.
- `external/gact-tui/apps/web/screenshots/audit/debug-rail-*.png`
  - Internal operator surfaces, not first-impression product shots.
- `external/gact-tui/apps/web/screenshots/audit/brand-*.png`
  - Branding QA shots, not workflow/product evidence.
- Any screenshot showing repeated routing errors, provider setup failures,
  unsupported-agent errors, raw JSON walls, or redacted debug payloads.

## Retakes Still Needed

The current set is usable for drafting the install page, but the release-quality
set should add two fresh captures:

1. CLIO-branded TUI running an EarthScope/NDP workflow.
   - Must show a real prompt, streamed work, final answer, and files/artifacts.
   - Must not show GACT branding, raw event dumps, or stale debug phrasing.

2. Native desktop app running the same EarthScope/NDP workflow.
   - Must show the conversation plus generated plot or Markdown artifact.
   - Must avoid empty states, settings panels, and routing/provider errors.

## Page Order

1. Hero: `ndp-earthscope-live-final.png`
2. Install cards: CLI install command, desktop downloads, Docker
3. Carousel:
   - `ndp-earthscope-live-artifact-preview.png`
   - `overnight-real-markdown-preview.png`
   - `overnight-real-image-preview.png`
   - `overnight-real-file-editor-diff.png`
   - `overnight-real-streaming-final.png`
4. Desktop section:
   - `desktop-linux-xvfb-chat-clio-18190-attached-1440.png` only as a temporary
     native-shell placeholder.

Keep the page language user-facing. Do not name internal release tracks,
issue numbers, "six paths", debugging history, or implementation details.
