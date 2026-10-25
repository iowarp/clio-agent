# Windows qualification with Codex sign-in

Run: 2026-10-24/25, America/Chicago (trace timestamps are UTC). **Overall: fail.** Live backend successes do not erase the failures below. No OpenAI API key was available.

## Ongoing semantic qualification

The 48-hour investigation started at **2026-10-25 04:50:27 UTC** and ends at
**2026-10-27 04:50:27 UTC**. It investigates whether people can accomplish useful
work across the product, expanding from discoveries and contradictions. The
earlier mechanism receipts below establish only their stated scope. They do not
establish research quality, autonomous orchestration, useful planning, or an
acceptable overall experience. Windows CI was already known to pass.

The first substantial journey asked the shipped Deep Researcher for a defensible
storage decision for a climate collaboration: 2 TB of arrays growing by 250 GB a
month, a 40 GB observation table, 15 xarray/pandas users, shared storage now and
object storage later, with a restriction against retaining three array copies.
The request required inspected primary sources, reconciled tradeoffs, a phased
migration, measurable validation, and a saved report. It named no tools or flow.

**Result: failed before producing a report.** In session
`sess_3068a44f70c2`, Codex / `gpt-6.1-sol` autonomously selected `deep-research`.
The planner separated baseline HDF5/NetCDF viability from Zarr implementation and
layout questions, with explicit evidence requirements and cautions against
universal performance claims. These are useful planning observations; the
research and recommendation remain unqualified.

Concurrent research startup hit [#1855](https://github.com/iowarp/clio-agent/issues/1855):
writers shared a resolved-plugin staging filename. One replaced it and another
raised `FileNotFoundError` / WinError 2. The flow session
`sess_ea22642cf09416bd60fe39619e6e86cd` failed while two accepted researchers
continued. The parent accurately said no verified report had been produced.
Later, five document-fetch permission requests appeared beneath that finished
failure. Cancellation of the surviving children was requested and their terminal
states captured; no duplicate research run was assumed from observation timeouts.

The repair gives each cache writer its own staging file and serializes immutable
publication under the existing store lock. Unique filenames alone were
insufficient on Windows: synchronized replacements reproduced WinError 5. The
concurrent regression and all nine plugin-store checks pass with serialized
publication. On the loaded repair, the original conversation retained its brief
and launched all six researchers successfully. Research quality and final
delivery still require verification.

The retry exposed a consequential handoff defect,
[#1857](https://github.com/iowarp/clio-agent/issues/1857). The first critic
received only later addenda for directions d1, d4 and d5, although their child
transcripts contain substantive baseline, capacity/migration and benchmark
packets. For example, d5's handoff contained only 336 characters of provenance
closure while its transcript contains an earlier 18,826-character benchmark
packet. Public text classified as `next_thought` was omitted from the final
prediction's `answer`, which the flow forwards as evidence. The critic correctly
reported missing evidence and launched six follow-up branches. This is useful
independent scrutiny, but also avoidable repeated work. The runtime/output
contract is under investigation; no repair or successful final research outcome
is claimed for this defect yet.

The same journey produced 17 source-reading permission requests. The old cards
hid source URLs and intent inside JSON. [GACT PR #681](https://github.com/iowarp/gact-tui/pull/681)
shows those facts before technical details and retains accessible consent
controls, with desktop/phone rendered review. Its subsequent folder-disclosure
and save-label refinements still need final live repetition. Choosing workspace
consent released only one request, leaving 16 covered reads parked in the same
workspace ([#1856](https://github.com/iowarp/clio-agent/issues/1856)). Those reads
were individually approved as a recorded workaround. The backend repair now
applies saved grants through the existing matcher/resolution path, with tests
covering scope, higher-priority asks/denies, special gates and source-grant
provenance. Loading and live-repeating that repair remains outstanding.

The earlier queued-budget failure also reproduced with the real task layer:
all three prepared children ran after the first spent the budget. The repair
checks recorded child-subtree spend when work is admitted after its execution
slot opens. Direct and nested-flow regression cases now refuse the later
children. This is a stop-admitting-work budget; it cannot retract tokens already
spent by an in-flight request. Live re-verification of the recorded budget
scenario remains outstanding.

The isolated runtime initially had no search backend. Its typed diagnostic
identified that condition; it is a setup limitation, not evidence of bad research
reasoning. Through the normal Infrastructure UI, native SearXNG was installed in
the disposable root and reached ready state. A subsequent normal stop retired
every worker and descendant in the observed creation-identity record. Installation
took about 24 seconds after target inspection. Its progress UI incorrectly called
a search-service step "Prepare the model"; permission-request clarity and
late attention after a failed flow remain product concerns to investigate and fix.

Evidence under the disposable root: `research-storage-plain.txt`,
`evidence/research-submitted.json`, recursive `evidence/journey-research-*.json`
(complete conversations, model requests, task trees, and flow views),
`evidence/shots/research-failed-1440-0-result.png`,
`evidence/searxng-process-observation.json`, and
`evidence/searxng-first-stop.json`. The evolving charter and execution state are
`GOAL-48H.md` and `CAMPAIGN-STATE.json` there.

## Failures first

| Defect | Issue | Disposition |
| --- | --- | --- |
| Research critic receives only addenda while substantial earlier public answers remain in child transcripts | [clio-agent#1857](https://github.com/iowarp/clio-agent/issues/1857) | Live semantic failure; repair under investigation |
| Workspace consent leaves matching parallel tool requests parked | [clio-agent#1856](https://github.com/iowarp/clio-agent/issues/1856) | Source repaired; boundary checks passed; loaded live repeat pending |
| Research permission cards hide source and intent in JSON | [gact-tui#680](https://github.com/iowarp/gact-tui/issues/680) | Source repaired in companion PR #681; rendered before/after; final refinements pending live repetition |
| Concurrent research startup races while pinning the same plugin resolution | [clio-agent#1855](https://github.com/iowarp/clio-agent/issues/1855) | Repaired; concurrent regression passed; six real research branches launched on retry |
| Token budget admits queued calls after exhaustion, even with concurrency one | [clio-agent#1849](https://github.com/iowarp/clio-agent/issues/1849) | Source repaired; direct/nested queued admission checks passed; live repeat pending |
| Settings-added MCP service missing from new turns until restart | [clio-agent#1852](https://github.com/iowarp/clio-agent/issues/1852) | Open; restart recovery verified |
| Modern MCP list-change event does not refresh the callable toolset | [clio-agent#1853](https://github.com/iowarp/clio-agent/issues/1853) | Open; independent subscribed control passed |
| One shell call asks Allow once twice | [clio-agent#1850](https://github.com/iowarp/clio-agent/issues/1850) | Open; real permission receipts retained |
| Failed flow card says Not started yet | [gact-tui#677](https://github.com/iowarp/gact-tui/issues/677) | Open; depth/model-call backend limits passed |
| Fleet rows lose model results and delegation model | [gact-tui#678](https://github.com/iowarp/gact-tui/issues/678) | Open; backend selection passed |
| Serialized calls captioned 2 in parallel | [gact-tui#679](https://github.com/iowarp/gact-tui/issues/679) | Open; concurrency limit itself passed |
| Standard Windows test isolation generates unsupported IPC/snapshot path lengths | [clio-agent#1851](https://github.com/iowarp/clio-agent/issues/1851) | Open; shorter paths within the same disposable root used for validation |
| Hashing includes excluded environments and submodule .git files, causing repeated installation and slow first conversations | [clio-agent#1847](https://github.com/iowarp/clio-agent/issues/1847) | Fixed here; shared traversal/copy boundary, pruned before descent |
| Relative local plugin install cannot update from another working directory | [clio-agent#1848](https://github.com/iowarp/clio-agent/issues/1848) | Fixed here; canonical local source plus legacy normalized URL recovery |

## Checkout and containment

Existing checkouts were updated in place, with **no Git worktrees**:

- clio-agent: `f3016b51afe27a197cec15cd317ea6d641b841af` from develop; fixes/report on `codex/windows-codex-qualification`.
- external/gact-tui and the existing standalone gact-tui checkout: develop `2692126d38a8c92b27fec750351aa3c85880de93`.
- external/clio-agent-marketplace: develop `1b5280c95097374a5585b5a25a7932f37f88d149`.

The pre-existing edit to `docs/design/agent-facing-async-tasks.md`, the GACT untracked brand configuration, and updated submodule pointers are excluded from this change. Backups and original heads are retained in the disposable root.

Every qualification runtime, generated workspace, session, fixture, log, screenshot, dependency, browser profile and cache is beneath:

`D:\Libraries\Documents\projects\clio-workspace\windows-codex-20261024`

`environment.ps1` redirects HOME/USERPROFILE, APPDATA/LOCALAPPDATA, TEMP, XDG, CODEX_HOME, all CLIO roles, uv/npm/pip/ruff/mypy caches and build output there. Installed executables and the existing sign-in were read from their normal locations; no intentional C: writes were made. The sign-in copy stays private in this root and is not committed. A Python write audit guard rejects out-of-root runtime writes, while the separate native fence probe checks the actual Windows child boundary. This is not a machine-wide filesystem audit of the Codex desktop application's own bookkeeping.

The web UI was built from an ordinary source archive under this root and driven in real Chrome at **1440x900** and **390x844**, in light mode. It used the live local server and real Codex inference, not replay data. This qualifies Windows server/browser behavior; it does not claim a Tauri packaged-desktop build test. No machine-wide UAC setup was needed because the bundled native Codex fence was already usable.

## Non-conversation scenarios

For these rows, session/provider/model/prompt are **N/A**; the command or UI action is the stimulus.

| Scenario | Result | Stimulus and evidence under the disposable root |
| --- | --- | --- |
| Native Windows write fence | pass | `uv run --frozen --no-sync python -B fence_probe.py`: native PowerShell child could write inside its territory and was refused outside with `GetContentWriterUnauthorizedAccessError`. Both probe targets were inside the disposable root. `evidence/fence-probe.json`, `logs/fence-probe-final.log`. |
| Default marketplace installation | pass after fix | Default service installation from existing marketplace checkout. Original source contained over 13k generated environment files; fixed hashing now matches snapshot exclusions. `evidence/paths.json`, marketplace state and plugin Settings screenshots. |
| Local plugin install/trust/update | pass after fix | Installed qualification-marker from a local folder, trusted, changed executable content, updated, observed pending_trust and UI Trust update, explicitly retrusted. `evidence/plugin-absolute-update.json`, `plugin-retrust.json`; relative-source regression covers new and legacy state. |
| Authored path security | pass | Actual authored.write calls with backslash, drive-letter, UNC and `:stream` paths returned typed `path_outside_plugin` before writes. `evidence/security-authored-paths.json`. |
| Marketplace plugin check | pass | `uv run --frozen --no-sync clio-agent plugin check <source> --json` on shell, deep-research and document-runtime; all ok=true. `evidence/plugin-check-{shell,deep-research,document-runtime}.json`. Initial nonexistent default-agent directory was a harness selection error, retained separately. |
| CLI MCP add/check/remove | pass | Added qualification-cli with the root's Python and mcp_fixture.py. Check reported ready/two tools; its reveal tool executed in a conversation. Remove printed removed qualification-cli. `evidence/mcp-cli-check-final.txt`, `mcp-cli-remove.txt`, complete-mcp-modern.json. |
| Settings MCP connect/remove | fail | Actual Connect MCP local-command form succeeded; new-turn exposure failed until restart (#1852). Removal returned removed=true. `evidence/mcp-ui-config.json`, `mcp-ui-remove.json`. |
| Modern MCP fixture control | pass | MCPServer + real `subscriptions/listen` client, negotiated 2026-07-28: ToolsListChanged received, late_add discovered, returns 40. `evidence/mcp-subscription-proof.json`. Initial FastMCP unsolicited path delivered no client notification and is not counted as a CLIO refresh proof. |
| Composed prompt view | pass | Open agent > Prompt; search ORCHID, inspect one matching fragment, expand it. Owner qualification-marker shown. Desktop and mobile collapsed/expanded screenshots inspected. |
| Account/model inventory | pass | Model picker shows Codex and all seven account models; fleet API account health ready. Settings Fleet itself configures role defaults; account inventory is in Usage/model picker. `evidence/fleet-final.json`, fleet-details model-picker screenshots. |
| Codex live usage | pass | Usage view shows the observed weekly window and credits. Final API source=`codex_rate_limit_event`, used_percent=27, resets_at=2026-11-01T03:44:27Z; earlier inspected screenshots show 21%. These are observations at different times. No raw HTTP-header capture is claimed for this transport. |
| OpenAI API-key usage gap | blocked: no API key | Not exercised; existing [#1837](https://github.com/iowarp/clio-agent/issues/1837) retained, no duplicate issue. |
| Exhaustion/fallback | blocked: no cheap exhaustion path | Account retained substantial weekly capacity and credits; no deliberate quota drain or simulated limit counted as live acceptance. |

## Conversation scenarios

Every parent below used **codex / gpt-6.1-sol**, reasoning low. Child models are noted explicitly. Exact initial prompts follow the table. Every tool-directed row is **mechanism verified with a tool-directed prompt; not evidence of agent judgement** (or a failed/blocked mechanism where indicated). Only baseline, orchestration-plain and fleet-plain are plain requests. For rows combining API and UI, any failed required component makes the row fail.

Each `evidence/complete-<scenario>.json` contains the exact prompt, parent and child sessions, full messages, semantic traces including actual `lm.call` requests/results, task tree, frame metadata and flow endpoint responses. A flow endpoint 404 for an ordinary React task is expected; it is not automatically classified as a defect.

| Scenario | Session | Result | Finding |
| --- | --- | --- | --- |
| baseline | `sess_409e8641c966` | pass | Plain request; returned 391. Initial marketplace scan failure is recorded separately below. |
| orchestration-plain | `sess_828faecd55d7` | pass | Plain request; chose a direct Parquet recommendation with qualifications. No orchestration was selected; this does not prove spontaneous council/arena use. |
| fleet-plain | `sess_427ea7ce03a0` | fail | Correct 391 via one PowerShell arithmetic call, but two Allow once gates for that call (#1850). No child model was selected; the answer honestly said its exact model identifier was unavailable. |
| council | `sess_6ada5da11a07` | pass | Three real children: gpt-6.1-sol, gpt-6-luna, gpt-5.6-luna; all returned 391. Synthesis used gpt-6.1-sol. Flow API and UI completed. |
| arena | `sess_69b90c59e89b` | pass | Two real contestants: gpt-6-luna and gpt-5.6-luna, both 323. Judge gpt-6.1-sol selected entry 1. Vote artifact retained in workspace/arena/votes.jsonl. |
| workflow-approval | `sess_4c8e93e32a30` | blocked: invalid harness input | Initial editors=[critique] named a step instead of a target; no editor tool was granted. Superseded by the corrected scenario, not scored as a product failure. |
| untrusted-workflow | `sess_a506c726fb92` | pass | Untrusted qualification_sum requested Trust and run before execution; after approval the step returned 40. Final flow API and reloaded UI completed. Initial early checkpoint 404 was retained. |
| workflow-approval-corrected | `sess_39819134a7dc` | pass | editors=[self] enabled a proposed polish step. A durable Question was answered Yes; the node records approval and all three steps completed. UI says You approved adding the step polish. |
| plugin-now | `sess_3bc15188dd2a` | pass | UI Yes applied now. Baseline LM request had no marker; next system message contained ORCHID-731 and the answer returned it. |
| plugin-compact | `sess_4010586360d8` | pass | UI Yes, after compaction submitted apply=compact. The subsequent system message contains ORCHID-731; the answer returned it. This was a short conversation, not a large-context compaction stress test. |
| plugin-note | `sess_a44a9bb33637` | pass | UI Yes, as an injection kept the marker out of the system message and supplied it in user context with a plugin-change note; answer ORCHID-731. |
| plugin-no | `sess_a82f4ba6943e` | pass | UI No sent no agent PATCH; no marker was present in the LM request and the answer was NONE. |
| limit-calls | `sess_5123a0d8ec11` | fail | Backend correctly returned recoverable flow_limit_exceeded / max_model_calls. UI incorrectly says Not started yet (#677). No refused call retried. |
| limit-budget | `sess_c43f69ced45a` | fail | budget_tokens=1 allowed both gathered calls. Followed by a serialized probe to separate admission from already-running work (#1849). |
| limit-concurrency | `sess_91dd5dae51a6` | fail | Backend max_concurrency=1 passed: first child completed 04:16:00.406162Z before second started 04:16:01.794044Z. UI still labels the group 2 in parallel (#679). |
| limit-depth | `sess_5cf36a19111e` | fail | Backend correctly refused depth 2 above 1 with recoverable flow_limit_exceeded / max_depth. UI incorrectly says Not started yet (#677). |
| restart | `sess_0d393a61278b` | blocked: invalid harness input | Initial child answered in prose instead of holding a durable question; typed call_awaiting_input was retained. Replaced by restart-held. |
| restart-held | `sess_a3afbbe3fd95` | pass | Restart while the child waited on a durable Question. Same sessions, handles, lineage and one restart-held artifact entry recovered; typed needs-inspection uncertainty was shown, then the existing child completed after Yes. No replay/duplicate. This does not test interruption during provider inference. |
| budget-serial | `sess_7f6988a8f581` | fail | budget_tokens=1 plus max_concurrency=1 still admitted the second call after the first consumed about 19k input tokens; both children returned 2 and 3 (#1849). |
| cancellation | `sess_87f0ab63b5d9` | pass | Cancelling the active hold flow cancelled its child flow and pending Question; parent collected a confirmed terminal result. The assistant said before its question appeared, but the retained pre-cancel API proves a question was pending. |
| mcp-refresh | `sess_d5c161c237ca` | fail | Settings service probed ready but was absent from a new conversation and its next turn (#1852). CLI namespace reveal tool ran after explicit permission. Initial FastMCP unsolicited-notification attempt is not accepted as valid modern subscription proof. |
| fleet-selection | `sess_59482e2ba17a` | fail | Backend list_models returned seven models; explicit child gpt-6-luna completed 899 with confirmed receipt. UI List models says No provider results and spawn is Inspect without the selected model (#678). |
| mcp-modern | `sess_38a805d343d6` | fail | After restart, Settings echo returned MCP-ORCHID-731 and CLI reveal returned late_add announced. A real modern subscription control receives ToolsListChanged and late_add=40, but CLIO did not expose late_add to its agent (#1853). |

## Restart and permission receipts

The qualifying restart's `/v1/sessions` readiness was **35.424 seconds**; the subsequent MCP-only restart was **29.694 seconds**. Initial corrected startup was **34.770 seconds**. The first restart held `sess_e9d9f01a0b42feac4924dfa01437f5ed` behind Question `task_854c5158bbb2bce74af1943815b07fb425b470e3cddc48aba4168c449940e3cf`; its parent flow retained `task_1dd1b8cc2d2cdf8dd599fa729b867bb8076b6a97b50b4a6da0b02fbc48a744fa`. The initial recovered UI exposed uncertainty rather than inventing completion. The original child subsequently completed; final API flow status is completed and its artifact entry occurs once. `restart-artifacts-before.jsonl`, `tree-restart-held-before.json`, `complete-restart-held.json`, process receipts and `restart-final` screenshots preserve the comparison.

The workflow's added polish node carries a durable approval question and accepted answer, visible in its flow view. Untrusted executable code waited for Trust and run. MCP once-permission responses were limited to the three explicitly requested qualification tools; the initial permission state was captured in both viewport sizes. Cancellation occurred while the hold flow and its child were open with a pending question, and all three became terminal.

`evidence/plugin-model-requests.json` preserves the actual request message arrays: now/compact put the marker in system; note supplies a user-context injection while leaving the system unchanged; no supplies neither. The follow-up after restart retained the same distinction. Merely seeing the correct answer was not used as the sole proof.

## Screenshot index

All paths below are relative to `<disposable-root>/evidence/shots`. Each listed evidence image was opened and visually inspected. Mobile captures sometimes retain a 1440 prefix from the browser launch; the trailing viewport or name and the actual PNG dimensions identify the capture. Transition/setup-failure images remain for audit but are not used to judge layout.

| Coverage | Desktop and mobile evidence |
| --- | --- |
| Baseline | `baseline-1440-0-result.png`, `baseline-390-0-result.png` |
| Plugin choices | `final-views-1440-0-choices-1440.png`, `final-views-1440-1-choices-390.png` |
| Each plugin outcome | `plugin-{now,compact,note,no}-final-1440-3-result.png` and `...-2-response-mobile.png` |
| Plugin installation/retrust | `plugin-settings-1440-0-result.png`, `plugin-updated-390-0-result.png` (different stages of the same scenario) |
| Prompt search/expand | `composed-prompt-1440-{0-search-desktop,1-expanded-desktop}.png`, `composed-mobile-390-{0-search,1-expanded}.png` |
| Council, arena, approved/untrusted workflow | `final-batch-1440-0` through `7` (scenario and viewport in filenames); approval pending/completed also `workflow-approved-390-*` |
| Plain requests, fleet selection | `final-batch-1440-8` through `13`; `fleet-details-1440-{0-delegation-1440,1-delegation-390}.png` |
| Model-call/depth/concurrency/budget limits | `final-batch-1440-14` through `21` |
| Cancellation | `cancellation-pending-1440-*`, `final-batch-1440-{22-cancellation-1440,23-cancellation-390}.png` |
| Restart | `restart-recovered-1440-0-result.png`, `restart-answer-390-*`, `restart-final-1440-{0-desktop,1-mobile}.png` |
| MCP Settings | `mcp-connected-1440-{1-connected,2-connected-mobile}.png` |
| MCP missing tools/restart recovery | `mcp-result-1440-{0-desktop,1-mobile}.png`, `mcp-modern-pending-1440-{0-desktop,1-mobile}.png`, `mcp-modern-final-1440-{0-desktop,1-mobile}.png` |
| Usage/models/accounts | `final-batch-1440-{28-settings-usage-1440,29-settings-usage-390}.png`; `fleet-details-1440-{2-models-1440,3-models-390}.png`; `final-views-1440-{4-providers-1440,5-providers-390}.png` |

The initial invalid workflow/restart probes are not UI acceptance claims. The parallel-budget attempt is superseded by the stronger serialized-budget proof. Browser resize reset some panels and early menu screenshots caught transitions; stable replacements above were inspected before scoring.

## Fix validation and remaining boundaries

- **153 plugin tests passed, zero skipped**, 33.00 seconds: `logs/plugins-tests-final.log`. New regressions cover digest/copy parity, pruning before traversal, no repeated default installation, and relative-source updates from another directory for new/legacy state, including executable retrust.
- Ruff check passed; Ruff format check reported six files already formatted. Mypy passed for the new package boundary and store (`logs/plugin-mypy.log`).
- Two existing assertions were made path-portable: hook argument path comparison uses Path equality, and the included-file expectation uses the native joined path. Neither check was removed.
- Standard test bootstrap first failed private clio-core IPC binding; shortening only TEMP then exposed snapshot WinError 206/3. A full test source snapshot at `<root>/a`, runtime parent `t`, and IPC temp `<root>/t` passed. The owned runtime cleanup remained enabled. No tests were skipped and the original failures remain recorded under #1851.
- UI build succeeded in the contained source snapshot. This is focused local validation and live acceptance evidence, not a claim that full repository CI or packaged desktop release validation passed.
- Harness corrections: invalid initial low-level MCP API usage, nonexistent default-agent plugin directory, plural prompt directory, editor step-vs-target input, incorrect initial PowerShell executable path, and extended Windows path normalization in the audit guard. They were corrected before scoring; they are not product issues. Early audit refusals in write-refusals.jsonl were for extended-prefix paths inside the root.

## Cleanup and reproducibility

`environment.ps1`, `supervise.py`, `server_entry.py`, `containment.py`, `api.py`, `scenarios.py`, `inspect_scenario.py`, `collect-complete.py`, fixtures and screenshot scripts remain under the disposable root. Each service has a PID/create-time custody receipt. Final shutdown uses those owned process identities; `evidence/cleanup.json` records the post-shutdown inspection. CLI and Settings MCP test entries were removed. The disposable root is retained for review and can be deleted later to remove local qualification state. Repository fixes/report and the intentionally created GitHub issues/PR remain; provider usage already incurred is not reversible.

## Exact prompts

These are the initial prompts, verbatim, keyed to the sessions above. All parent provider/model values are codex/gpt-6.1-sol. The two corrected harness scenarios retain their original attempts in the table.

### baseline

Session `sess_409e8641c966`.

```text
What is 17 multiplied by 23? Give the answer in one sentence.
```

### orchestration-plain

Session `sess_828faecd55d7`.

```text
Our lab is deciding whether to use CSV or Parquet for a 40 GB numerical dataset that five people filter by a few columns. What should we choose, and what could change the recommendation? Keep the answer concise.
```

### fleet-plain

Session `sess_427ea7ce03a0`.

```text
Please get me a reliable answer to 17 times 23 while keeping model usage economical, and tell me which model did the work.
```

### council

Session `sess_6ada5da11a07`.

```text
This is a mechanism qualification. Use run_flow with flow council for the question "What is 17 times 23? Give a one-sentence answer." Use three self members, with models codex/gpt-6.1-sol, codex/gpt-6-luna, and codex/gpt-5.6-luna. Wait for the result, report the answer and the models that actually ran. Do not write files or browse the web.
```

### arena

Session `sess_69b90c59e89b`.

```text
This is a mechanism qualification. Use run_flow with flow arena for "What is 19 times 17? Answer in one sentence." Use two self contestants on codex/gpt-6-luna and codex/gpt-5.6-luna. Wait for completion, report the winner and the actual models. Do not browse or run shell commands.
```

### workflow-approval

Session `sess_4c8e93e32a30`.

```text
This is a mechanism qualification. Use run_workflow with steps draft (target self, input "Write one sentence explaining 17 times 23."), critique (target self, after draft, input "You are the editor. Call workflow_add_step to add exactly one step with id polish, target self, input Verify the arithmetic in one sentence, and after draft. Then finish your critique."). Set editors to [critique], allowed_targets to [self], max_added_steps to 1, max_edit_rounds to 1, approve_edits true. Wait for completion. Do not browse or run shell commands.
```

### untrusted-workflow

Session `sess_a506c726fb92`.

```text
This is a mechanism qualification. Use run_workflow with a single step id sum, target qualification_sum, input [17,23]. This installed external plugin is untrusted: let the permission question appear before executing anything. Wait for the result after approval. Do not browse or use shell commands.
```

### workflow-approval-corrected

Session `sess_39819134a7dc`.

```text
This is a mechanism qualification. Use run_workflow with steps draft (target self, input "Write one sentence explaining 17 times 23; do not add steps."), critique (target self, after draft, input "You are the editor. Call workflow_add_step to add exactly one step with id polish, target self, input Verify the arithmetic in one sentence, and after draft. Then finish your critique."). Set editors to [self], allowed_targets to [self], max_added_steps to 1, max_edit_rounds to 1, approve_edits true. Wait for completion. Do not browse or run shell commands.
```

### plugin-now

Session `sess_3bc15188dd2a`.

```text
Say ready. Do not use any tools.
```

### plugin-compact

Session `sess_4010586360d8`.

```text
Say ready. Do not use any tools.
```

### plugin-note

Session `sess_a44a9bb33637`.

```text
Say ready. Do not use any tools.
```

### plugin-no

Session `sess_a82f4ba6943e`.

```text
Say ready. Do not use any tools.
```

### limit-calls

Session `sess_5123a0d8ec11`.

```text
This is a mechanism qualification. Use run_flow with flow qualification_limits, input "qualification limit-calls", options {"max_model_calls": 1}. Wait for completion and report typed outcomes without retrying a refused call. Do not browse or run shell commands.
```

### limit-budget

Session `sess_c43f69ced45a`.

```text
This is a mechanism qualification. Use run_flow with flow qualification_limits, input "qualification limit-budget", options {"budget_tokens": 1}. Wait for completion and report typed outcomes without retrying a refused call. Do not browse or run shell commands.
```

### limit-concurrency

Session `sess_91dd5dae51a6`.

```text
This is a mechanism qualification. Use run_flow with flow qualification_limits, input "qualification limit-concurrency", options {"max_concurrency": 1}. Wait for completion and report typed outcomes without retrying a refused call. Do not browse or run shell commands.
```

### limit-depth

Session `sess_5cf36a19111e`.

```text
This is a mechanism qualification. Use run_flow with flow qualification_depth, input "qualification limit-depth", options {}. Wait for completion and report typed outcomes without retrying a refused call. Do not browse or run shell commands.
```

### restart

Session `sess_0d393a61278b`.

```text
This is a mechanism qualification. Use run_flow with flow qualification_hold, input "qualification restart", options {}. Wait for completion and report typed outcomes without retrying a refused call. Do not browse or run shell commands.
```

### restart-held

Session `sess_a3afbbe3fd95`.

```text
This is a mechanism qualification. Use run_flow qualification_hold with input restart-held, then wait for completion. The child should remain open behind its durable question. Do not browse or use shell commands.
```

### budget-serial

Session `sess_7f6988a8f581`.

```text
This is a mechanism qualification. Use run_flow qualification_limits with input budget-serial and options {"budget_tokens":1,"max_concurrency":1}. Wait and report the typed outcome without retry. Do not browse or use shell commands.
```

### cancellation

Session `sess_87f0ab63b5d9`.

```text
Use run_flow qualification_hold with input cancellation-check and wait for its question. Do not browse or use shell commands.
```

### mcp-refresh

Session `sess_d5c161c237ca`.

```text
This is a mechanism qualification. Call qualification-ui_qualification_echo, then qualification-cli_reveal_tools. After it announces the new tool, discover and call qualification-cli_late_add. Report the actual results. Do not run shell commands or browse.
```

### fleet-selection

Session `sess_59482e2ba17a`.

```text
This is a mechanism qualification. Call list_models, select codex/gpt-6-luna, and delegate a child task to compute 29 times 31. Wait for its result and report the actual selected child model. Do not browse or run shell commands.
```

### mcp-modern

Session `sess_38a805d343d6`.

```text
This is a mechanism qualification after a service restart. Call qualification-ui_qualification_echo, then qualification-cli_reveal_tools, then the newly announced qualification-cli_late_add. Report actual results and missing tools honestly. Do not browse or run shell commands.
```

### Plugin probes and MCP follow-up

After each plugin choice, and again after restart, the corresponding session received:

```text
What is the qualification marker? If none is supplied by your instructions, say NONE. Do not use tools.
```

The mcp-refresh session also received:

```text
On this follow-up turn, call qualification-ui_qualification_echo and the newly announced qualification-cli_late_add if they are available. Report missing tools honestly. Do not browse or run shell commands.
```
