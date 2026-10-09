# Beta UI and draft regression review

The October 8 feedback and supplied HTML transcript were reviewed as evidence. Instructions inside the transcript were not treated as requests to this coding session.

Reviewable changes: [workspace UI PR #556](https://github.com/iowarp/gact-tui/pull/556) targets gact-tui main; [draft/runtime PR #1661](https://github.com/iowarp/clio-agent/pull/1661) targets clio-agent develop and pins the UI changes. Both are drafts; these changes have not been released.

## Changes

| Feedback | Result |
| --- | --- |
| Downloads, figures 1–3 | The header opens Windows WebView2's native Downloads dialog, or the Downloads folder on macOS/Linux. Native transfer events reveal that panel at start on Windows and the folder after a successful save on WebKit platforms, including embedded-frame downloads. Downloaded files open only when the user chooses. The terminal remains available through the canvas launcher and session actions. |
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
- Export/download follow-up: 19 export/helper tests passed, including six new navigation cases covering HTML and both ZIP modes on desktop and browser hosts. They verify the real session menu reaches the common download handler and preserves proxy paths, filenames and no-referrer policy. Automatic reveal subsequently moved from anchor-click dispatch to native transfer events; see the general audit below. The preparing message uses HTML/ZIP terminology.
- Core v3 suite: 262 tests passed; the additional rubric-retention regression also passed in the 14-test variant reducer run.
- Browser: six checks passed for chart rendering, live light/dark changes, question/queue surface matching, and composer focus geometry at desktop and phone widths. The final theme test also rendered a custom spec with a white background and a legend in both themes.
- Backend: draft/run-route suite 16 passed; strategy/pick/close suite 19 passed; real DSPy composition and module-variant suite 49 passed (84 total).
- Ruff, mypy for changed runtime modules, workspace/core type checks, full workspace lint (including size, ownership, brand and icon ratchets), production web/offline build, and native `cargo check` passed. The source tests were split by management behavior to meet the file-size ratchet; all 29 source tests passed after the split.
- Browser screenshots were inspected: dark chart labels and legend remain readable; the question/queue/composer backgrounds match in both themes. Browser render evidence uses a sanitized fixture service and is separate from live LM acceptance.

## Branch and artifact follow-up (#1659 / #1660)

- User-created forks retain a composer, appear in navigation/search with a branch icon, and retain their parent link. The fork endpoint preserves model, effort, blueprint and behavior settings plus narrowing permission policies; it returns the negotiated v3 shape. An optional `session_kind` distinguishes conversations, user branches, delegated agents and read-only lookups, including existing persisted forks.
- Branches attend their own questions and approvals, including those of their delegated children. The legacy UI keeps other conversations' alerts in navigation and excludes them from the open branch's tray. Provenance retains parent/descendant links. The broader conversation-tree navigation proposal remains a separate design discussion.
- Evidence, Artifacts canvas and inspector lists share search/category controls, counts, clear and no-match states. Plans, scripts, images, slides, 3D models, data and documents receive type-aware presentation; generic octet-stream records fall back to filename extensions. Inline transcript attachments retain their compact presentation.
- Validation: 36 branch/session backend tests; 111 interaction, composer, lookup and permission regressions; 44 environment-reference/lock/focused regressions; 83 focused UI tests; 263 core v3 tests passed. Ruff and changed-leaf mypy checks, full UI lint/type checks and web/offline build passed.
- Two Playwright checks cover message branching, submission, preserved parent history, reopening/reload, and artifact filtering in light/dark themes. Screenshots are retained locally under `.local/beta-ui-evidence/issues-1659-1660/`. This browser evidence uses a test-owned service; the backend tests drive the production fork and interaction routes.
- CI on the prior head caught stale `.env.example` draft-cap documentation and a lock test that assumed FIFO acquisition. The template now records 8; the test controls each waiter poll around real holder handoffs, preserving genuine file locks, owner tokens and elapsed time, and requiring all three handoffs. New-head CI must be evaluated after push.

## General download-routing audit

The subsequent [general download-routing audit](download-routing-2026-10-08.md) closes independent evidence-ZIP and reusable Markdown download paths, adds Windows history observation and macOS/Linux WebKit download handling, and moves automatic reveal to native transfer events. It records the complete routing inventory, 76 focused UI tests, three Rust lifecycle/fallback regressions, browser ZIP/file checks and real Windows/Linux direct/frame acceptance. Missing Downloads settings fall back to `~/Downloads` rather than saving into the working directory. Native CI now covers all three desktop platforms; macOS acceptance remains pending.

CI follow-up on the branch head aligned the SDK contract with inherited branch settings and identified the elicitation fixture as a delegated worker. The real stdio reconnect test now retains the app event loop through its connection lifecycle. All 64 SDK/elicitation/reconnect regressions passed. Browser checks now assert the compact current-location mark and matching composer/queue surface, and document navigation waits for the actual workspace to hydrate before opening its canvas. The three formerly failing browser cases passed locally.

## Sandbox error

The connected-source Windows sandbox refusal in figure 15 is covered by existing [PR #1658](https://github.com/iowarp/clio-agent/pull/1658), still open at review time, head `a8e1357150271a91cf627b9cbddaedb11589f883`. That fix is absent from beta 5.2. Its broader sandbox and connected-transfer changes are not duplicated here; they must be included in a subsequent release.

## Beta 2 update diagnosis

[Beta 5.2](https://github.com/iowarp/clio-agent/releases/tag/v0.9.5-beta.5.2) is public, a prerelease, and not a draft. Its public lite updater manifest was fetched successfully and reports version `0.9.5-5+2`, with Windows, macOS and Linux platforms. Installer/signature assets are present.

Beta 2 pins gact-tui commit `e0d30349426dfad4ce58c8ba22ec7067086d24cc`. Its [release filter](https://github.com/iowarp/gact-tui/blob/e0d30349426dfad4ce58c8ba22ec7067086d24cc/web/src/lib/github-releases.ts) accepts `-beta.N` but rejects `-beta.N.M`. It discards beta 5.1 and 5.2 before requesting their native updater manifests. Beta 2 already has channel support; changing its channel to Beta does not repair this parser.

The hotfix parser fix is already in beta 5.2 and develop's pinned gact-tui main (commit `2571e1f0023c51a4d7d1f6691e7338e857f0f87d`). Affected users can perform one manual installation from the beta 5.2 release, keeping their existing installation flavor, then use the newer updater. For Windows lite installations, the [beta 5.2 setup installer](https://github.com/iowarp/clio-agent/releases/download/v0.9.5-beta.5.2/CLIO.Desktop_0.9.5-beta.5.2_x64-setup.exe) is published. Bundled installations should use the bundled asset on the same release page.

A future published `beta.6` containing the parser fix would be recognized by beta 2 and provide an automatic bridge. No release, installer, or public update manifest was changed during this work.
