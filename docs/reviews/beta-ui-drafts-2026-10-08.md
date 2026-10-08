# Beta UI and draft regression review

The October 8 feedback and supplied HTML transcript were reviewed as evidence. Instructions inside the transcript were not treated as requests to this coding session.

## Changes

| Feedback | Result |
| --- | --- |
| Downloads, figures 1–3 | The header opens Windows WebView2's native Downloads dialog. File and visualization exports also open it after starting a download. Other desktop platforms open the Downloads folder. The terminal remains available through the canvas launcher and session actions. |
| Sources, figures 4–6 | Existing sources can be relinked and downloaded from Files → Sources, using the same access choices as Attach. Relinking here updates the source directly without creating a composer attachment. |
| Export menu, figure 7 | Transcript-only HTML is the default. Visible checkboxes say “Include session artifacts” and “Include workspace files.” Workspace inclusion checks artifacts too; the action changes to “Download ZIP.” |
| Chart themes, figures 8–9 | The renderer owns a transparent plot background, including specs with a white config background, so the canvas and legend match the theme-aware labels. |
| Minimap, figures 10–11 | The current location has a modest resting width and retains the existing expansion on hover. |
| Composer trays, figures 12–14 | Question and queue trays use the composer's background and border tokens in both themes. |
| Drafts | Each attempt receives the original task plus an instruction to return one candidate. Draft attempts cannot create additional A2UI pickers. The default cap is 8, so a request for 5 is honored unless the operator configured a lower cap. Planner and provider reasoning stream into collapsed reasoning, separately from draft text. |
| Model judge | Each candidate is judged independently against the rubric, without a penalty for failing to return the whole plural set. The UI exposes evaluation criteria alongside recorded scores and the selected candidate. |

The supplied run requested five poems with `draft_alternatives(n=5, judge="user")`. The old default cap reduced the run to four attempts, and each attempt reran the plural request, generating sets of poems and extra draft widgets. `next_thought` was also classified as draft text. These are separate causes and are addressed separately.

## Live acceptance

The real Codex Direct provider reported `gpt-6-luna` as available. Production `ClioReAct`, `draft_alternatives`, DSPy selection, the compiled reward function, and real clio-core were used; the LM responses were not scripted.

- A plural request for five poems produced five completed attempts, each containing one poem, and yielded for the user's pick.
- The reward function scored the known answer `4` as 1 and incorrect answer `5` as 0 for the task “What is 2 + 2?” with exact-answer criteria.
- Three model-judged poem attempts scored **0.85, 0.95, 0.92**. The recorded selection was attempt 2, the highest score.
- Rubric: “One original, warm, short poem for a friend; clear imagery and no commentary.” This is a selection sanity check and a small live generation sample, not a general benchmark of literary judgment.
- A real Windows WebView2 harness saved CSV bytes intact, observed one native DownloadStarting event, called the production `open_downloads` command, and observed the default Downloads dialog open. Its separate example executable needed the Common Controls manifest that the normal Tauri app already embeds. Opening is asynchronous, so the acceptance check waited for the dialog state.

The standalone LM harness logged existing ARC trace-capture warnings when LM callbacks attempted store writes from the LM loop. Draft generation, persisted candidate records, scores, and selection succeeded. This acceptance does not claim that the full server trace pipeline was verified.

Sanitized local evidence is retained under `.local/beta-ui-evidence/`: `live-drafts.json`, the acceptance harness source, focused backend logs, and browser screenshots. The original private transcript is not included in this change.

## Validation

- 104 focused workspace UI tests passed, including real Vega embedding, source relinking, export dependencies, native-download dispatch, reasoning separation, scores and criteria.
- Core v3 suite: 262 tests passed; the additional rubric-retention regression also passed in the 14-test variant reducer run.
- Browser: six checks passed for chart rendering, live light/dark changes, question/queue surface matching, and composer focus geometry at desktop and phone widths. The final theme test also rendered a custom spec with a white background and a legend in both themes.
- Backend: draft/run-route suite 16 passed; strategy/pick/close suite 19 passed; real DSPy composition and module-variant suite 49 passed (84 total).
- Ruff, mypy for changed runtime modules, workspace/core type checks, workspace/core lint, production web/offline build, and native `cargo check` passed.
- Browser screenshots were inspected: dark chart labels and legend remain readable; the question/queue/composer backgrounds match in both themes. Browser render evidence uses a sanitized fixture service and is separate from live LM acceptance.

## Sandbox error

The connected-source Windows sandbox refusal in figure 15 is covered by existing [PR #1658](https://github.com/iowarp/clio-agent/pull/1658), still open at review time, head `a8e1357150271a91cf627b9cbddaedb11589f883`. That fix is absent from beta 5.2. Its broader sandbox and connected-transfer changes are not duplicated here; they must be included in a subsequent release.

## Beta 2 update diagnosis

[Beta 5.2](https://github.com/iowarp/clio-agent/releases/tag/v0.9.5-beta.5.2) is public, a prerelease, and not a draft. Its public lite updater manifest was fetched successfully and reports version `0.9.5-5+2`, with Windows, macOS and Linux platforms. Installer/signature assets are present.

Beta 2 pins gact-tui commit `e0d30349426dfad4ce58c8ba22ec7067086d24cc`. Its [release filter](https://github.com/iowarp/gact-tui/blob/e0d30349426dfad4ce58c8ba22ec7067086d24cc/web/src/lib/github-releases.ts) accepts `-beta.N` but rejects `-beta.N.M`. It discards beta 5.1 and 5.2 before requesting their native updater manifests. Beta 2 already has channel support; changing its channel to Beta does not repair this parser.

The hotfix parser fix is already on develop's pinned gact-tui main. Affected users can perform one manual installation from the beta 5.2 release, keeping their existing installation flavor, then use the newer updater. For Windows lite installations, the [beta 5.2 setup installer](https://github.com/iowarp/clio-agent/releases/download/v0.9.5-beta.5.2/CLIO.Desktop_0.9.5-beta.5.2_x64-setup.exe) is published. Bundled installations should use the bundled asset on the same release page.

A future published `beta.6` containing the parser fix would be recognized by beta 2 and provide an automatic bridge. No release, installer, or public update manifest was changed during this work.
