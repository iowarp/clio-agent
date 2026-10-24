# Notebook theme: ledger

The record for [notebook-theme-plan.md](notebook-theme-plan.md). Each row has an ID, the behavior the plan requires, the evidence and a status. Decisions taken while implementing carry the prefix `T-D`.

Work happens in `iowarp/gact-tui` on branch `claude/notebook-theme`. It was built on `b4067ce` (`fix(web): Child agents and Background tasks say none running, not 0`) and is now stacked on `claude/long-term-memory` (iowarp/gact-tui#674) at `506b5815`, so the PRs merge in sequence (T-D17).

## Rows

| ID | Required behavior | Evidence | Status |
| --- | --- | --- | --- |
| T1 | Phase 0 inventory recorded | [T1 inventory](#t1-phase-0-inventory) below | Passed |
| T2 | Switching Light, Dark and Notebook in settings changes every screen, with no reload and no leftover colors | [T2](#t2-switching-themes) | Passed |
| T3 | Notebook follows the system light or dark setting, and reload shows no flash of the other variant | [T3](#t3-system-scheme-and-first-paint) | Passed |
| T4 | Code, a table, a chart, a workflow railroad and status chips render legibly in each theme at 1440x900 and 390x844 | [T4](#t4-screens-in-every-theme) | Passed |
| T5 | Every text and tint pair meets WCAG AA | [T5](#t5-measured-contrast) | Passed |
| T6 | No hex colors in `web/src` outside the theme definitions | [T6](#t6-no-colors-outside-the-theme) | Passed |
| T7 | The debug theme shows no element that skips the core theme | [T7](#t7-debug-theme-audit) | Passed |


## Qualification

Final run on Oct 24 2026, on gact-tui `claude/notebook-theme` at `0ce8862d` (stacked on `claude/long-term-memory`, T-D17; the same six theme commits now sit on #674's newer tip as `6dd0a22f` to `da4900ba`, see below), with this clio-agent branch stacked on clio-agent `claude/long-term-memory` (iowarp/clio-agent#1835) so the memory page has its server. The stack was started with `bash scripts/cloud/stack.sh restart` (agent with the `claude_code` provider on `claude-haiku-5-5`, gact-tui dev server). One command reruns it: `bash scripts/cloud/checks/theme_qualify.sh`, which drives `node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/theme_<row>.mjs` at 1440x900 and 390x844 and writes `.cloud-run/theme-<row>-<size>.json`. Every run in the final pass reported `consoleErrors: []` and `failedRequests: []` (T3 at 390x844 hit one `page.reload` timeout of the dev server on the first pass and was rerun clean), and every screenshot of it (169) was read as contact sheets. The conversation is a live session in which the agent answered with a Python code block and a markdown table.

The first pass on the stacked branch, before clio-agent was stacked too, reported 404s for `/v1/memory/*`: the memory page from iowarp/gact-tui#674 calls endpoints that only clio-agent `claude/long-term-memory` serves. Nothing in them was about the theme.

gact-tui gates on `0ce8862d`: `pnpm lint` (including the theme color guard) passed, `pnpm typecheck` passed, `pnpm test` passed 57 core files (308 tests) and 573 web files (4032 tests), and `pnpm build` passed. On the first CI run of the stacked branch, every job passed except the browser suite, which failed two tests in one of its two runs and one in the other (163 and 164 of 165 passed). Both were timing races, present on the base too. The Customize popover's drag handle was measured before the popover was positioned: it renders unplaced for one or two frames, measured at the same ~90 ms and ~135 ms on this branch and on its base. Collapsed sidebar icons were measured while the menu buttons were still animating into the rail. The memory session fixed both on #674 (`7a0cdef1`: collapsed buttons no longer animate, and the drag waits for settled handles), so the theme commits were rebased onto that tip instead of carrying their own test changes. On the rebased tip `da4900ba` the gates passed again (`pnpm test`: 308 core and 4033 web tests). With the build the suite expects (`CLIO_REVIEW_FIXTURES=1`), both spec files passed 30 of 30 over three repeats.

**Follow-up commit `49f6e85d`.** The gact-tui Playwright suites (which CI runs as its visual baselines) were then run locally against the pinned Chromium. Components (16) and deployment (2) passed. The browser e2e suite (159 tests) failed six. Three of them asserted next-themes behavior (its `theme` storage key, the `light` class, three theme choices) and were updated. The other three (two visual baselines and a virtualized-scroll test) fail the same way on the base commit `b4067ce` in this container, which renders differently from the baselines' machine. Comparing this branch's rendering with the base's, pixel by pixel in the same container, showed one real difference: the token shadow scale had made Dark's shadows slightly darker. Light and Dark now set `--shadow-color` to black at 50%, which reproduces Tailwind's shadows exactly. After that fix, the flat-NDP and summarization renderings match the base's exactly, and the mobile one differs only by a scroll button that appears in some runs on both branches. T2 and T4 at 1440x900 were rerun on `49f6e85d` (all four variants: no reload, 0 leftover elements of 448, 42 screenshots read, no console errors or failed requests). Nothing else changed after the full run. That was on the earlier base; the e2e checks it updated came along in the rebase.

Screenshots: [before and after at 1440x900](screenshots/before-after-1440.png), [before and after at 390x844](screenshots/before-after-390.png), [railroad states](screenshots/railroad-states.png), [Debug theme](screenshots/debug-theme.png).

### T2: switching themes

`theme_t2_switch.mjs` opens the conversation, goes to Settings through the app's own navigation (sidebar link, then Appearance; on a phone the sidebar sheet and the section menu), picks each theme with the system scheme set to match, and goes back. `performance.timeOrigin` is unchanged, so no reload happened. It then snapshots every visible element's computed color, background, border, fill, stroke and font, reloads the same screen, and compares.

| Size | Light | Dark | Notebook light | Notebook dark |
| --- | --- | --- | --- | --- |
| 1440x900 | no reload, 0 of 453 elements differ | no reload, 0 of 453 | no reload, 0 of 453 | no reload, 0 of 453 |
| 390x844 | no reload, 0 of 238 elements differ | no reload, 0 of 238 | no reload, 0 of 238 | no reload, 0 of 238 |

### T3: system scheme and first paint

`theme_t3_scheme.mjs` picks Notebook, then switches the emulated system scheme light, dark, light without a reload: `<html>` follows each time (`notebook`, then `notebook` with `dark`, then `notebook`). It then reloads under each scheme with an init script that records every change to `<html>`'s theme attributes and every paint entry from the start of the document.

| Size | System | Theme applied at | First paint at | States showing another variant |
| --- | --- | --- | --- | --- |
| 1440x900 | light | 238 ms | 3556 ms, `notebook` light | 0 |
| 1440x900 | dark | 202 ms | 3276 ms, `notebook` dark | 0 |
| 390x844 | light | 168 ms | 3280 ms, `notebook` light | 0 |
| 390x844 | dark | 107 ms | 3008 ms, `notebook` dark | 0 |

### T4: screens in every theme

`theme_t4_screens.mjs` shows, in Light, Dark, Notebook light and Notebook dark, at both sizes: the live conversation (code block and table), the widget gallery's chart, workflow, status chip, data table, code and map, the workflow railroad fixture (T-D13) and the memory settings page. That is 36 screenshots per size, all read and legible. The railroad nodes take the exact status token values, for example Notebook light done `rgb(220, 235, 231)` / `rgb(31, 79, 75)`, running `rgb(242, 227, 196)` / `rgb(110, 78, 18)`, cancelled `rgb(230, 225, 214)` / `rgb(94, 89, 79)`, with failed on `destructive`. The A2UI workflow nodes are colored by state (completed, running) in every theme. The agent's prose is in Literata under Notebook, and the interface is in IBM Plex Sans.

### T5: measured contrast

`theme_t5_contrast.mjs` resolves each token in the browser, composites any alpha over the page, and computes the WCAG ratio for 41 pairs per variant: ink, secondary and muted text on every surface; text on the accent and its tint; links; each status label on its tint (4.5:1) and each status icon on the card (3:1); outcome text; terminal, syntax and highlight text. All 164 measurements pass. The lowest are Light 4.57 for text and 3.09 for icons; Dark 5.34 and 6.35; Notebook light 4.69 and 5.01; Notebook dark 4.96 and 6.80. Two Dark values failed on the first measurement and were fixed (T-D11).

| Pair | Light | Dark | Notebook light | Notebook dark |
| --- | --- | --- | --- | --- |
| `foreground` on `background` | 17.60 | 17.67 | 12.52 | 13.28 |
| `card-foreground` on `card` | 18.38 | 16.86 | 13.54 | 11.97 |
| `muted-foreground` on `background` | 6.50 | 7.65 | 5.04 | 5.72 |
| `muted-foreground` on `card` | 6.79 | 7.30 | 5.45 | 5.16 |
| `primary-foreground` on `primary` | 6.43 | 10.44 | 5.50 | 5.97 |
| `accent-foreground` on `accent` | 14.17 | 14.55 | 7.50 | 6.78 |
| `link` on `card` | 5.92 | 10.14 | 5.50 | 6.73 |
| supported label on tint | 7.46 | 9.13 | 7.50 | 6.78 |
| running label on tint | 6.42 | 8.89 | 6.00 | 6.50 |
| refuted label on tint | 6.14 | 7.96 | 6.84 | 6.22 |
| superseded label on tint | 6.12 | 7.74 | 5.34 | 5.84 |

The Notebook status ratios match the style guide's published figures exactly.

### T6: no colors outside the theme

`node scripts/check_theme_colors.mjs` in gact-tui (also run by `pnpm lint`): `Theme color guard passed: no color values outside web/src/theme.` A raw grep over the 1083 `.ts`, `.tsx` and `.css` files in `web/src` outside `web/src/theme`, tests and fixtures (on `0ce8862d`) finds no hex value containing a letter, and the only all-digit matches are issue numbers in comments (such as `#1533` and `#1549`). The guard's patterns were checked against samples: it catches `'#ffffff'`, `text-amber-600`, `dark:text-amber-400`, `bg-white/10` and `shadow-[0_1px_rgb(0_0_0/0.1)]`, and ignores `rgb(${r} ...)`, `color-mix(in oklch, var(--x) 5%, transparent)` and `href='#top'`.

### T7: Debug theme audit

`theme_t7_debug.mjs` picks Debug in Settings and, under both system schemes, audits 15 screens: Settings, the conversation, the gallery's chart, workflow, status, data table, code, map, metric, steps, callout and diff, the railroad, Runs and the memory settings page. For every visible element and its `::before` and `::after`, it checks the computed color, background, borders, outline, SVG shape fill and stroke, box-shadow and gradient colors (each resolved to sRGB, and each must be a saturated red or transparent), and that text is in the marker face. Images, canvases (map tiles), KaTeX math and `data-theme-exempt` data scales are exempt.

| Size | Screens | Elements checked | Elements off the theme |
| --- | --- | --- | --- |
| 1440x900 | 30 | 5482 | 0 |
| 390x844 | 30 | 3448 | 0 |

Before the fix in `d205192e`, the audit found maplibre's controls and attribution on maplibre's own white, black and Helvetica (11 findings per map screen). The screenshots show every screen in shades of red, except the OpenStreetMap tiles, which are images.

## Decisions

**T-D1. Base branch.** The work is based on the commit this branch pins (`b4067ce`, on `claude/sdk-plugins-flows`), not on `develop`. Develop is 27 commits past the merge base and the pin is 58 commits past it, and the two trees differ in 568 files. A develop-based branch would drop the SDK UI that this clio-agent branch's server needs, and it could not be checked against this stack. The draft PR therefore targets `claude/sdk-plugins-flows`, so its diff holds only the theme commits. The commits touch only theme code and the components that read it, so they can be cherry-picked onto `develop` later. (Superseded for the PR's base by T-D17: the branch is now stacked on the memory PR.)

**T-D2. Debug theme (added at the owner's request).** gact-tui was designed so that every component reads one core theme. The inventory below shows that rule has drifted. To make drift visible and keep themes easy to add, the theme is a single token contract (`web/src/theme/`), and Light, Dark, Notebook and a **Debug** theme each supply values for the whole contract. Debug sets every color token to a shade of red and every font token to a marker face. Anything on screen that is not red, or not in the marker face, skips the core theme. A DOM audit (`debugAudit` in `scripts/cloud/checks/theme_lib.mjs`, run by `theme_t7_debug.mjs`) reads the computed color, background, border, fill, stroke and font of every element under Debug and reports anything that is not a Debug value. That is row T7, added to the plan. Debug appears in the selector only in development builds.

**T-D3. Fonts are the Google Fonts families, served from the app bundle.** Literata, IBM Plex Sans and IBM Plex Mono come from the Fontsource packages of the Google Fonts files (`@fontsource-variable/literata`, `@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-mono`), the same way the app already ships Inter and JetBrains Mono. Linking `fonts.googleapis.com` would break in the desktop app, whose CSP is `style-src 'self' 'unsafe-inline'; font-src 'self' data:`, and in offline or HPC installs, and it would add a third-party request to every page. Faces load only when a theme uses them. The fallback stacks are the style guide's. The plan's Type section now says so.

**T-D4. The selector keeps System.** Settings offers System, Light, Dark and Notebook. System already existed and still follows the OS between Light and Dark; Notebook follows it between its own two variants. The command menu gains "Switch to Notebook theme". The plan's Selector paragraph now says so.

**T-D5. One mechanism: next-themes is gone.** The appearance provider stores the theme in `clio.appearance.v1` with the other preferences, reading the old next-themes `theme` key once so an existing choice carries over. `<html>` carries `data-theme` (the resolved theme) and the `dark` class (the variant), which `themes.css` and Tailwind's `dark:` variant read. A classic boot script, built from `web/src/theme/boot.ts` by `web/vite-plugin-theme.ts`, applies the stored theme before first paint; the desktop CSP's `script-src 'self'` allows it, which next-themes' inline script did not. Review fixtures use `ForcedTheme`.

**T-D6. Brand tokens restyle only Light and Dark.** `App.tsx` used to write `brand.themeTokens` and `brand.accent` as inline styles on `<html>`, which overrode every theme. The brand plugin now injects them before first paint as `html:root:is([data-theme='light'], [data-theme='dark'])`, so the product brand keeps its action color in the default themes while Notebook and Debug keep theirs. The unused `--brand-accent` is gone.

**T-D7. How run states map to science states.** Completed and succeeded use supported; running and recovering use running; cancelled uses superseded; failed stays `destructive`, because a failure is an error and refuted is a finding. Refuted has no run state today; the tokens ship for the study tree, which is not on this base. A2UI workflow nodes map their free-text state the same way.

**T-D8. What counts as a color outside the theme (T6).** The guard `scripts/check_theme_colors.mjs` (in gact-tui, run by `pnpm lint`) fails on hex literals, literal `rgb()`/`hsl()`/`oklch()` values and Tailwind palette utilities in `web/src`, outside `web/src/theme`. Comments are skipped, since issue references like `#1549` look like hex. Tests and test fixtures are out of scope: they carry colors as data, for example a chart spec an agent might send. Scientific colormaps (viridis, turbo) are data encodings and live in `web/src/theme/data-scales.ts`, the same in every theme. Gmail and Outlook marks moved to `branding/provider-logos/`.

**T-D9. Renderers that cannot read CSS variables resolve tokens.** Vega, Mermaid and Pele, maplibre, three.js and canvas code call `readThemeColor()` in `web/src/theme/runtime.ts`, which resolves a token to sRGB in the applied theme, and they redraw when `useThemeKey()` changes. Shiki highlighting uses a CSS-variable theme (`--syntax-*`), so code follows a theme change without re-tokenizing.

**T-D10. Light links use the link token.** Markdown, citation and table links read `--link`. In Light that is the theme's info blue (5.92:1 on the card), where they had used the primary teal. Dark keeps its primary.

**T-D11. Two Dark text tokens fixed.** T5 measured Dark `destructive-foreground` (red-600) at 4.10:1 and `info-foreground` (violet-600) at 3.32:1 on the card. Both moved to the 400 shades (above 4.5:1). Nothing else in Light or Dark changed value.

**T-D12. Focus ring.** `--ring` is the accent itself. Components already apply their own alpha (`ring-ring/50`, `outline-ring/50`), which gives the plan's "accent at 40 to 50 percent".

**T-D13. The railroad is checked on a review fixture.** A real workflow run needs a plugin flow. The only one in the marketplace, the wildfire review, depends on external data services. `web/tests/review/flow-states.html` renders the real `FlowDiagram` from a parsed `FlowRun` with steps done, running, failed, cancelled and waiting. The conversation, gallery chart, workflow, status, table, code and map are real app screens.

**T-D14. Code highlighting does not run in the cloud dev server.** On `b4067ce` as well as on this branch, the gallery and transcript code blocks render as plain text in the cloud dev server; Shiki's tokens never arrive. Shiki produces `var(--syntax-*)` colors when called directly, and the syntax tokens are defined and measured (T5). This is unrelated to the theme and left as is.

**T-D15. Dead theme code deleted.** The eleven Mermaid preset palettes (`lib/mermaid-themes.ts`, reachable only by a custom theme name no caller passed), the unused shadcn Recharts wrapper (`components/ui/chart.tsx`) and the `recharts` dependency, next-themes, and the inline brand-style loop in `App.tsx`.

**T-D16. The GitHub release check is stubbed during qualification.** The app lists `api.github.com/repos/iowarp/gact-tui/releases` for its update notes. From the cloud's shared egress that unauthenticated call is intermittently rate-limited (HTTP 403, "rate limit exceeded"), which put a console error into some runs that has nothing to do with the theme. The qualification steps answer that one request with an empty list (`quietReleaseChecks` in `theme_lib.mjs`, installed through a new optional `setup(page)` export that `ui_check.mjs` runs before the first page load), so `consoleErrors` reports the UI under test. Nothing else is stubbed; the agent, the model and every other request are live.

**T-D17. Stacked on the memory PR.** The PR's first base, `claude/sdk-plugins-flows`, moved 34 commits past `b4067ce` (including a develop merge), and the PR conflicted. To merge with the memory work in sequence, the five theme commits were rebased onto `claude/long-term-memory` (iowarp/gact-tui#674, `16852545`) and the PR retargeted to it. Three files conflicted, where the base had rewritten code the theme had converted: the data-table error state (moved into `data-table-request-state.tsx`, whose link now uses `text-link`), the context canvas palette (removed on the base) and the offline basemap (now a coarse land outline, whose water, land and coastline read `muted`, `card` and `border` and repaint on theme change). The base also added colors the guard rejects: the file badges in `reui/file-type.tsx` (about 120 palette utilities plus literal white and black), the context usage bar, artifact and model icons, the memory diff view and terminal detail text. A sixth commit maps each palette hue to its nearest category or chart token (blue to `category-1`, violet and purple to `category-2`, orange to `chart-2`, and so on), white and black to `invert-foreground` and `invert`, diff lines to the success and destructive foregrounds, and terminal text to `terminal-muted`. File badges carry their type label, so merging near hues (sky with cyan, rose with red) loses no information. T4 and T7 now also cover the memory settings page.

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

Found after Phase 0, by the guard in Phase 1: `lib/search-passage.ts` (search highlight colors, now `--highlight`) and `components/reui/number-field.tsx` (`#0008` drop shadow and a black and white cursor glyph, now `--shadow-color`, `fill-foreground` and `stroke-background`). The Phase 0 grep had skipped all-digit hex values as issue references.

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
