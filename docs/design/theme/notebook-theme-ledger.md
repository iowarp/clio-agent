# Notebook theme: ledger

The record for [notebook-theme-plan.md](notebook-theme-plan.md). Each row has an ID, the behavior the plan requires, the evidence and a status. Decisions taken while implementing carry the prefix `T-D`.

Work happens in `iowarp/gact-tui` on branch `claude/notebook-theme`. Its base is `b4067ce` (`fix(web): Child agents and Background tasks say none running, not 0`), the commit this clio-agent branch pins.

## Rows

| ID | Required behavior | Evidence | Status |
| --- | --- | --- | --- |
| T1 | Phase 0 inventory recorded | [T1 inventory](#t1-phase-0-inventory) below | Passed |
| T2 | Switching Light, Dark and Notebook in settings changes every screen, with no reload and no leftover colors | | Pending |
| T3 | Notebook follows the system light or dark setting, and reload shows no flash of the other variant | | Pending |
| T4 | Code, a table, a chart, a workflow railroad and status chips render legibly in each theme at 1440x900 and 390x844 | | Pending |
| T5 | Every text and tint pair meets WCAG AA | | Pending |
| T6 | No hex colors in `web/src` outside the theme definitions | | Pending |
| T7 | The debug theme shows no element that skips the core theme | | Pending |

## Decisions

**T-D1. Base branch.** The work is based on the commit this branch pins (`b4067ce`, on `claude/sdk-plugins-flows`), not on `develop`. Develop is 27 commits past the merge base and the pin is 58 commits past it, and the two trees differ in 568 files. A develop-based branch would drop the SDK UI that this clio-agent branch's server needs, and it could not be checked against this stack. The draft PR therefore targets `claude/sdk-plugins-flows`, so its diff holds only the theme commits. The commits touch only theme code and the components that read it, so they can be cherry-picked onto `develop` later.

**T-D2. Debug theme (added at the owner's request).** gact-tui was designed so that every component reads one core theme. The inventory below shows that rule has drifted. To make drift visible and keep themes easy to add, the theme is a single token contract (`web/src/theme/`), and Light, Dark, Notebook and a **Debug** theme each supply values for the whole contract. Debug sets every color token to a shade of red and every font token to a marker face. Anything on screen that is not red, or not in the marker face, skips the core theme. A DOM audit (`scripts/cloud/checks/theme_audit.mjs`) reads the computed color, background, border, fill, stroke and font of every element under Debug and reports anything that is not a Debug value. That is row T7, added to the plan. Debug appears in the selector only in development builds.

## T1: Phase 0 inventory

Taken on `b4067ce`, Oct 23 2026.

### Where Light and Dark live, and how a theme is chosen

- **Tokens.** `web/src/index.css`. `:root` holds Light and `.dark` holds Dark, as shadcn-style oklch custom properties (`--background`, `--foreground`, `--card`, `--primary`, `--muted`, `--accent`, `--destructive`, `--info`, `--success`, `--warning`, `--action`, `--chart-1..5`, `--sidebar-*`, `--composer*`, `--invert*`, `--radius`). `@theme inline` maps them to Tailwind `--color-*` utilities. Fonts are hardcoded in `@theme inline` (`--font-sans` and `--font-heading` are Inter Variable, `--font-mono` is JetBrains Mono Variable), loaded through `@fontsource-variable/*` imports at the top of the file.
- **Dark variant.** `@custom-variant dark (&:is(.dark *))`. 45 files use `dark:` utilities.
- **Choosing and storing.** `next-themes` `ThemeProvider` in `web/src/providers/app-providers.tsx` (`attribute="class"`, `defaultTheme="dark"`, `enableSystem`). It stores the choice under the `theme` localStorage key, apart from the other UI preferences, which `AppearanceProvider` stores under `clio.appearance.v1`. next-themes injects its own pre-paint script.
- **Selector.** `AppearanceSettings` in `web/src/routes/settings-page.tsx` offers System, Light and Dark. The command menu (`global-search-results.tsx`) also toggles the theme.
- **Second channel.** `App.tsx` copies `brand.themeTokens` and `brand.accent` onto `document.documentElement.style` at startup. The gact brand sets `accent: #5b8def`, which overrides `--action` as an inline style in every theme.
- **Readers of the theme name.** `mermaid-diagram.tsx`, `widget-preview.tsx` and `settings-page.tsx` read `resolvedTheme`. `sonner.tsx` reads `theme`. `use-chart-view.ts`, `a2ui-mesh-view-state.ts`, `html-preview-theme.ts` and `mcp-app-surface.tsx` read `classList.contains('dark')`. `ui/chart.tsx` keys colors by `light`/`dark`. `html-preview-theme.ts` already keeps a list of public tokens, which is a partial token contract.

### Hardcoded hex colors in `web/src` (excluding tests)

| File | Count | What it colors today | Becomes |
| --- | --- | --- | --- |
| `lib/mermaid-themes.ts` | 110 | Eleven Mermaid preset palettes (emerald, coral, ...), reachable only when a diagram config names a custom theme. `ClioMermaidDiagram` always uses `base`, so the presets are dead | Deleted |
| `components/clio/mermaid-diagram.tsx` | 12 | Mermaid theme variables for light and dark, and the font name | Resolved from `--primary`, `--card`, `--border`, `--foreground`, `--muted-foreground` and `--font-sans` at render time |
| `components/clio/a2ui-raster-viewport.tsx` | 26 | Scientific colormaps (viridis, plasma, cividis, turbo, ...) | Moved into the theme module as fixed data scales |
| `components/clio/category-colors.ts` | 12 | Category palette shared by charts and maps | `--category-1..12` tokens, resolved at runtime |
| `components/clio/map-category-palette.ts` | 4 | Uncategorized and continuous low, mid and high map colors | `--muted-foreground`, `--chart-*` tokens |
| `components/clio/chart-presentation.ts` | 3 | Vega axis ink, muted and grid colors, chosen by a dark flag | `--foreground`, `--muted-foreground`, `--border` |
| `components/ui/chart.tsx` | 1 | Recharts grid selector `stroke='#ccc'` (matches a library default, not a color we paint) | Kept as a selector, rewritten without a hex literal |
| `components/clio/scientific-map-view.tsx` | 5 | Point highlight, uncategorized point, polygon outline and point stroke | `--action`, `--muted-foreground`, `--card` |
| `components/clio/map-initial-view.ts` | 1 | Map background before tiles load | `--muted` |
| `components/clio/a2ui-message-draft.tsx` | 7 | Gmail and Outlook logo marks | Provider logo SVG files in `branding/provider-logos/` |
| `components/clio/observability-activity.tsx` | 6 | Causal-chain series colors | `--chart-1..5` and `--category-*` tokens |
| `components/clio/a2ui-region-capture.tsx` | 3 | Canvas selection stroke, fill and label | `--primary`, `--primary-foreground` |
| `components/clio/mesh-viewport-snapshot.ts` | 3 | Snapshot canvas background and caption | `--background`, `--foreground` |
| `components/clio/a2ui-mesh-view-state.ts` | 2 | Three.js feature-edge color by `.dark` | `--foreground` resolved to sRGB |
| `components/clio/mesh-viewport-formats.ts` | 1 | Default mesh tint | `--chart-1` resolved to sRGB |
| `components/clio/a2ui-workflow-catalog.tsx` | 2 | Mermaid `classDef selected` fill and stroke | `--accent-tint` and `--primary`, resolved |
| `components/clio/html-preview-bridge.ts` | 2 | Fallbacks inside `var(--foreground, #17252e)` and `var(--border, #aaa)` | Fallbacks removed; the bridge always sends the tokens |
| `components/clio/surface-export.ts` | 1 | Export background when the page is transparent | `--background` |
| `components/clio/mermaid-export.ts` | 1 | Export canvas background | `--background` |
| `components/mermaidcn/zoom-pan.tsx` | 1 + 1 rgba | Zoom selection rectangle | `--primary` |
| `widget-gallery.css` | 13 | Gallery hero and card tints | `--primary`, `--foreground` |

The other matches the naive grep finds are issue references (`#1549`, `#516`) and two JSON-schema descriptions in `test-fixtures`.

### Other hardcoded colors

- **Tailwind palette utilities:** 141 uses in 38 files. The largest are the terminal views (`ai-elements/terminal.tsx`, `tool-terminal-result.tsx`, `workspace-terminal-panel.tsx`, `tool-result-saved-output.tsx`, `operation-log-terminal.tsx`: zinc), `resource-pipeline-status.tsx` (amber and emerald by state), `surface-full-screen.tsx` (black and white), `a2ui-weather.tsx` (sky and amber), `context-canvas-panel.tsx` and `transcript-minimap.tsx` (one hue per kind), `connection-page.tsx`, `ai-elements/tool.tsx` (yellow, blue, green, orange, red by state), `search-highlight.tsx` and `file-search-passage.tsx` (blue highlights), `connected-source-detail.tsx`, `web-search-service-connection.tsx`, `session-usage-readout.tsx`, `composer-queue.tsx`, `a2ui-steps.tsx` (amber and emerald by state), and modal scrims (`bg-black/10`). They become `--terminal-*`, status, `--highlight`, `--scrim` and `--chart-*` tokens.
- **`dark:` utilities with palette colors** such as `dark:text-amber-400`. They become one token whose value differs per theme. `dark:` utilities that only adjust tokens (`dark:bg-input/30` in the shadcn primitives) are scheme variants, not theme branches, and stay. Notebook dark sets `.dark`, so they apply there too.
- **rgba shadows:** 4 arbitrary `shadow-[...rgba(...)]` values. They become `--shadow-color`.
- **Hardcoded font stacks:** `--pele-font` and `--pele-font-mono` in `index.css`, the Mermaid `fontFamily`, and the Vega `font`. They become font tokens.

### Components that show a status

| Component | Statuses | Colored today with |
| --- | --- | --- |
| `components/clio/status.tsx` (`ClioStatus` badge) | stream, tool and run states | `info`, `success`, `warning`, `action`, `destructive` and `muted` at 10 to 30 percent |
| `components/clio/task-activity-row.tsx` (task rows) | running, completed, failed, cancelled | `text-primary`, `text-success`, `text-destructive`, `text-muted-foreground` |
| `components/clio/async-task-list.tsx`, `shared-task-ui` | task states | through `ClioStatus` and the task row |
| `components/clio/flow-diagram.tsx` with `index.css` `.clio-flow-diagram .st-*` (workflow railroad) | done, running, waiting, failed, cancelled, proposed | `--success`, `--primary`, `--muted`, `--destructive` and `--warning` mixed into `--card` |
| `components/clio/flow-run-view.tsx`, `flow-run-card.tsx`, `flow-track.tsx` | step states | through the railroad and `ClioStatus` |
| `components/clio/resource-pipeline-status.tsx` | pipeline stages | amber and emerald palette utilities |
| `components/ai-elements/tool.tsx` | tool call states | yellow, blue, green, orange, red palette utilities |
| `components/clio/a2ui-steps.tsx` | step states | amber palette utilities |
| Study tree | not present on this base | the status tokens ship for it |
