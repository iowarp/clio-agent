# Acceptance ledger: Meta CLIO, the guide and the builder

Rows follow the "Meta CLIO: the guide and the builder" plan (guide G1 to G7, builder B1
to B8, verification 1 to 10). A row passes only with recorded evidence: tests,
commands, session ids, and for UI work the screenshots that were read. Status is
`passed`, `partial`, `failed` or `blocked`. Decisions use the prefix `MCD`.

Live rows ran on **Web through the cloud stack** (`bash scripts/cloud/stack.sh start`,
`CLIO_CLOUD_ISOLATE=1`, GACT on 8787, gact-tui on 5173, provider `claude_code`), not on
the Tauri desktop, which a cloud session cannot reach. UI rows were driven with
`node scripts/cloud/ui_check.mjs --steps <file>`; REST rows with a small driver that
posts messages, answers questions and accepts offers as the person would. The fixture
workspace `beamline` held synthetic HDF5 runs (`run_0412.h5`, `run_0413.h5`, Celsius in
`entry/sample/temperature`). Screenshots are under `.cloud-run/shots/` in the session
that ran them and are not committed.

Following the repository rule on live scenarios, a row marked "mechanism verified with a
tool-directed prompt" is not evidence of agent judgement.

## Decisions

| ID | Decision | Evidence | Status |
| --- | --- | --- | --- |
| MCD1 | `capture` is both the skill and the slash command. A separate `commands/capture.md` clashed with the skill (`command_skill_clash`), so the skill is the command. | `tests/test_plugins/test_builder_references.py`; `/capture` from the composer menu (`capture4-2-slash-menu.png`). | passed |
| MCD2 | Text-only plugins are never held for trust. Once `private-local` is trusted, `store._apply` holds only records with executable parts. | `657b0fa`; `tests/test_plugins/test_trust_boundary.py`. | passed |
| MCD3 | A variation of a plugin that brings tools or servers requires the original and adds only the change, still naming it in `variation_of`; a copy would collide with the original's tools. | `e2b097e`; live row 8 (`shell-beamline-h5` requires `shell`, `variation_of: clio/shell`). | passed |
| MCD4 | Guide answers carry the docs' screenshots as Markdown images with `plugin-asset://guide/captures/<name>.webp`, which render in chat offline. `clio.steps.v1` stays an option where the client advertises interactive views; a REST-only session advertises none and its A2UI surface is refused. | `657b0fa`; gact-tui `0487f1e`; row 2. | passed |
| MCD5 | The plugin review route maps an unknown plugin to a typed 404 (`LookupError`). | `tests/test_gact/test_plugin_routes.py`. | passed |

## Verification

| # | Scenario | Sessions | Evidence | Status |
| --- | --- | --- | --- | --- |
| 1 | Guide, narrow question | `sess_0fa83668ab0f` | Loaded `clio`, read `docs-map.md`, then `docs/permissions.md` and `docs/hooks.md` in one call; answer names both pages with their site addresses. | passed |
| 2 | Guide, wide question with steps and screenshots | `sess_ed2c5411bac7` (UI); earlier `sess_b639d651651f`, `sess_8b1c9dfdb01f`, `sess_ccb3b73ed2ae`, `sess_9775787162b6` | Final UI run delegated to `clio-guide` and showed four bundled captures inline (`guide-wide-ui2-2-after-send.png`). Deviations: the steps are a Markdown list, not `clio.steps.v1`; the Mermaid diagram rendered as a code block. Earlier runs answered in prose; the fixes are MCD4 and the skill's setup-advice trigger. | passed with deviations |
| 3 | Guide answers from the installed version, not the website | `sess_74d27df2cd63` | The live site has no `/docs/capture/` (404); the answer came from the bundled `docs-map.md` and `docs/capture.md`, with no web fetch. | passed |
| 4 | `/capture` from one session | `sess_afd0a7686a1a` | `/capture` from the composer (`capture4-2-slash-menu.png`, `capture4-3-after-send.png`) wrote `beamline-temperature` (skill, tool, fragment) to `private-local`; `capture-reviewer` review; v2; trial with `spawn_agents_parallel` and `plugins` add; trust asked once (`perm_efec56902a79`); added to the conversation. Finding: the control child offered itself the plugin under test. | passed |
| 5 | `/capture` from several `@`-named sessions, with a flow | `sess_5348b10e0136` (named `sess_4d2627c3b495`, `sess_b8e6af66d9e4`) | Both records read; one plugin `beamline-batch` (skill, check tool, fragment), reviewed, v2 installed. The agent chose not to write a flow, so "with a flow" is not met. | partial |
| 6 | `/optimize <plugin>` on a plugin Clio wrote | `sess_0ba42775fee2` | Scope shown first; records read; 0.2.0 then 0.2.1 (authorship versions 3 and 4, diff recorded, `executable_changed` only the tool); old-against-new trial; trust ask `perm_417d427d91d7` named `changed_since_trusted=['ai.iowarp.clio/tools/plot_temperature.py']`. | passed |
| 7 | `/optimize` falls through to capture for an uncovered procedure | `sess_8abecc2d9ad6` (evidence from `sess_533d7dbd05ff`) | Scope of 3 sessions; the cool-down fit was found but not proposed (one session, and declined in row 9's second run). On the person's plain reply that they fit every run, the agent said the plugin does not cover the procedure and asked the capture detail questions (overlay, asymptote, window, report) before writing; v0.3.0, review, v0.3.1 (authorship versions 5 and 6). It tested the fit in scratch copies with `run_code` and ran no side-by-side trial (`optimize7-2-before.png`). `/optimize` inside `sess_533d7dbd05ff` ended twice in a provider safety refusal (`ClaudeCodeSafetyRefusalError`, typed). | passed with deviations |
| 8 | `/optimize` on a built-in makes a variation; the original is untouched | `sess_160b44e003de`; earlier `sess_74284661efa5` | `shell-beamline-h5` requires `shell`, adds one fragment, `variation_of: clio/shell` on both versions, reviewed, offered and accepted; no old-against-new trial (the agent said so). `src/clio_agent/plugins/builtin/shell` unchanged. The earlier run wrote `shell-hdf5` without `variation_of`, fixed by MCD3. | passed |
| 9 | Global `/optimize`: scope, scan, report, choice, watermark, declined findings | `sess_1abde745e85e`, `sess_1eb03d74fd1d` | First run: 15 sessions (first run, 30 days), per-plugin report, `ChoicePicker`, `optimize_report_shown` with 2 declined, `lab-notes-runs` written as a variation of `private-local/lab-notes` after `name_taken_by_person`, offered and accepted; no trial. Second run: 13 sessions since the watermark, scanned through the `optimize-scan` flow (`run_flow`) from a plain `/optimize`; `beamline-batch-trial` not proposed again; `beamline-temperature-verify` proposed again because it recurred in new sessions; "None" moved the watermark to 17:20:02 and updated the existing key (5 recorded, 6 total) (`optimize9b-2-before.png`). The planned dataset (trigger miss, repeated correction, two-session gap, provider error) was not built; the report drew on the sessions of rows 1 to 8. | passed with deviations |
| 10 | Refusals: `name_taken_by_person`, `session_not_named`, `plugin_not_trusted` | `sess_b3910b99e129` | (a) `name_taken_by_person` reached on agent judgement and shown as a choice. (b) `session_not_named`: the agent asked for an `@` instead of trying; mechanism verified with a tool-directed prompt; not evidence of agent judgement. (c) `plugin_not_trusted`: the agent could not see the untrusted plugin; mechanism verified with a tool-directed prompt; not evidence of agent judgement (`refusals10-2-session.png`, "Delegated 1 task, 1 failed"). | passed (a); mechanism only (b, c) |

## Findings outside this change

- A visible reply in `sess_533d7dbd05ff` began with a literal `<thinking>` tag; the
  second global `/optimize` listed it as not fixable by a plugin.
- `offer_plugin` search does not find an untrusted marketplace plugin (`units-tools`),
  so the agent cannot offer it for trust (row 10c).

## Follow-up: views and the private marketplace

| # | Scenario | Sessions | Evidence | Status |
| --- | --- | --- | --- | --- |
| 11 | A plugin brings its own view as an MCP App | `sess_2bb6b4a48915` | The view reference's worked example (`run-board`: `mcp.json`, a standard-library server, a skill) installed into `private-local`, trusted, added to the conversation. A plain request ("Show me today's runs: ... Which went over?") opened the board inline: one card per run, red over the 360 K limit (`runboard-3-after-send.png`). `test_the_view_example_serves_an_mcp_app` runs the same server over stdio. | passed |
| 12 | A request for a new built-in view gets the pull-request answer | `sess_aa1ee8a30dfd`; earlier `sess_e56545b5c963` | Plain request for a built-in periodic table picker. After the capture skill's description named views, the agent loaded `references/view.md` and said a built-in component needs a change to CLIO, offered to open a pull request with GitHub access and the person's yes, said it waits for the maintainers' approval, and offered an MCP App or existing components meanwhile. The earlier attempt, before that trigger, read CLIO's source (the test workspace is inside the repository) and asked how to proceed without mentioning GitHub. | passed |
| 13 | The private marketplace shows its folders and backs up to GitHub | stack UI | Settings, Extensions and connectors, Marketplaces, Private local marketplace: "Saved here" lists the plugin; its menu has Back up with GitHub, Download marketplace ZIP and Marketplace details (folder paths) (`market4-5-click-3.png`, `market4-6-click-4.png`). The ZIP held the plugin's files; `test_a_plugin_clio_wrote_is_in_the_github_backup` covers a plugin written by `make_plugin`. The backup is manual (download, upload to a repository you create); CLIO does not push. | passed |
