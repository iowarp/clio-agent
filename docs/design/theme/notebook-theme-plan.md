# Notebook theme: implementation plan

This plan adds a third theme to gact-tui, called **Notebook**, beside the existing Light and Dark themes. Notebook keeps a warm paper and ink feel with a verdigris accent, and its colors double as the status vocabulary of the science log (supported, running, refuted, superseded). It has a light and a dark variant and follows the system setting to pick between them.

The work runs to completion without stopping for review. The human reviews the finished branch.

## Contract

This plan is the contract for the theme. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger with the prefix `T`.

| Document | Governs |
| --- | --- |
| This plan | The Notebook theme, its tokens, the theme selector and the status tokens |
| delegation-fork-science-plan.md (CLIO 1.0 project plan) | The study tree and its statuses, which consume the status tokens |

Base: the current gact-tui branch in use for CLIO 1.0, which is the commit the clio-agent branch pins (T-D1). The theme does not depend on the science log having landed. If the study tree exists, Phase 3 wires it to the status tokens; if not, the tokens ship and the study tree uses them when it is built.

## Rules

1. **One theme mechanism.** Notebook is a token block in the same place the Light and Dark themes are defined today. Do not add a second theming system, a per-component theme prop or a separate stylesheet loader.
2. **Components read tokens, never hex.** Any hardcoded color found in a component during Phase 0 is replaced with a token in the same change. No component may branch on the theme name.
3. **Status colors are tokens.** Supported, running, refuted and superseded each get a tint and a text token in every theme, including Light and Dark. The study tree, the log panel and any status icon read them.
4. **Not a Claude clone.** The accent is verdigris teal, not terracotta or clay, and nothing in the theme uses Anthropic names or values.
5. **Delete and replace.** If the existing theme code has a dead or duplicate path, remove it rather than keeping it next to the new one.
6. **One token contract, a debug theme to prove it.** Every color, font and radius token is named once in the theme module, and every theme supplies a value for each. A development-only **Debug** theme sets every color token to a shade of red and every font token to a marker face, so anything on screen that is not red, or not in the marker face, is not reading the core theme. Adding a theme means adding one more value set for the same contract. (Added by T-D2.)

## Design

**Selector.** Settings gains **Notebook** beside **System**, **Light** and **Dark**; System still follows the OS between Light and Dark (T-D4). Notebook follows `prefers-color-scheme` for its variant. The choice is stored with the other UI preferences and applied before first paint, so there is no flash of the wrong theme.

**Surface tokens.**

| Token | Notebook light | Notebook dark |
| --- | --- | --- |
| canvas (page) | `#F3EFE6` | `#1E1C19` |
| surface (cards, messages) | `#FBF8F1` | `#27251F` |
| surface raised (popovers, menus) | `#FFFDF8` | `#2F2C26` |
| code background | `#EDE7DA` | `#2A2823` |
| border | `#DDD6C8` | `#3A3630` |
| border strong | `#C9C0AE` | `#4A453D` |
| text primary (ink) | `#2B2A26` | `#E8E3D8` |
| text secondary | `#5E594F` | `#B3AC9F` |
| text muted | `#6B655A` | `#9C9588` |

**Accent tokens.**

| Token | Notebook light | Notebook dark |
| --- | --- | --- |
| accent | `#2F6F6A` | `#5FA8A0` |
| accent hover | `#255B57` | `#74B8B0` |
| on accent | `#FBF8F1` | `#16211F` |
| accent tint | `#DCEBE7` | `#21403D` |
| link | `#2F6F6A` | `#74B8B0` |
| focus ring | accent at 40% | accent at 50% |

**Status tokens.** Each status is a tint for the chip or node background and a text color for its label and icon.

| Status | Light tint / text | Dark tint / text |
| --- | --- | --- |
| supported (verdigris) | `#DCEBE7` / `#1F4F4B` | `#21403D` / `#9FD3CB` |
| running (ochre) | `#F2E3C4` / `#6E4E12` | `#4A3A1A` / `#E8C27A` |
| refuted (brick) | `#F0D9D2` / `#7A3022` | `#4A2A23` / `#E8A595` |
| superseded (graphite) | `#E6E1D6` / `#5E594F` | `#33302A` / `#B3AC9F` |

Light and Dark get the same four status tokens with values from their own palettes, so the status vocabulary is the same in every theme. Danger and warning keep their existing meaning; refuted is a science state, not an error, and stays visually distinct from the error color.

**Charts and diagrams.** The kernel's chart and the workflow railroad read the accent and status tokens for their series and node colors, so a chart in Notebook matches the study tree beside it.

**Type.** Literata for the agent's voice and headings, IBM Plex Sans for the interface, IBM Plex Mono for data and code, all Google Fonts families. The app serves their files from its own bundle (the Fontsource packages), because the desktop CSP and offline installs cannot load fonts.googleapis.com (T-D3). The style guide [notebook-style.html](notebook-style.html) shows the scale, components and the full token block, and it is the visual reference for T4.

## Phase 0: inventory

No production code changes in this phase.

1. Find where Light and Dark are defined and how the current theme is chosen, stored and applied.
2. List every hardcoded color in `web/src` outside the theme definition, with its file and the token it should become.
3. List the components that show a status (task rows, railroad nodes, the study tree if present) and what they color today.
4. Record the findings as T1 in the ledger, commit, and continue.

## Phase 1: tokens

1. Add the Notebook light and dark token blocks beside the existing themes.
2. Add the status tokens to all three themes.
3. Replace the hardcoded colors found in Phase 0 with tokens.

## Phase 2: selector

1. Add Notebook to the theme setting, with the system-following variant.
2. Apply the stored theme before first paint.

## Phase 3: status consumers

1. Point task rows, railroad nodes and the study tree at the status tokens.
2. Point the kernel chart's default series colors at the accent and status tokens.

## Phase 4: qualification

Restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

| ID | Scenario | Passes when |
| --- | --- | --- |
| T1 | Phase 0 inventory | Recorded in the ledger |
| T2 | Switch between Light, Dark and Notebook in settings | Every screen changes with no reload and no leftover colors from the previous theme |
| T3 | Notebook with the system set to light, then dark | The matching variant applies; reload shows no flash of the other variant |
| T4 | A conversation with code, a table, a chart, a workflow railroad and status chips in each theme | All render legibly; screenshots read at both sizes |
| T5 | Contrast check of every text and tint pair above | Each pair meets WCAG AA (4.5:1 for body text, 3:1 for icons and large text) |
| T6 | Grep `web/src` for hex colors outside the theme definitions | No matches |
| T7 | Debug theme: audit the computed colors and fonts of every element on each screen of T4 | Every element reads a Debug value; screenshots show only red and the marker face (added by T-D2) |

## Done

1. Notebook ships with its light and dark variants, the selector and the status tokens in all three themes.
2. No component contains a hardcoded color, and T6 passes.
3. T1 to T7 pass, each recorded in the ledger with the screenshots read.
4. The branch is pushed in gact-tui, the submodule pin in clio-agent points at it, and the draft PR carries before and after screenshots of each theme.

## Working conventions

- Branch `claude/notebook-theme` in gact-tui. Open a draft PR against the base branch.
- Small Conventional Commits per phase.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
