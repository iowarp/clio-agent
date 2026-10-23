# Ordinary-request planning and natural diagram outcomes

The October 18 follow-up was incomplete after the private service stopped. The
older “16 diagrams across 14 subjects” runs explicitly prescribed Mermaid,
component guidance, publication and image review. They qualify directed authoring
exercises, not spontaneous discovery. Their complete prompts remain in
[`mission.txt`](../../scripts/live_verification/mermaid_capabilities/mission.txt)
and [`holdout.txt`](../../scripts/live_verification/mermaid_capabilities/holdout.txt).

The resumed assessment contains **19 real CLIO sessions**: nine Sol 6.1 and ten
Luna sessions, including retained failed attempts and fresh retries. Eighteen
reached an answer, a plan approval or a consultation endpoint; one stopped on a
provider connection error. This completes this assessment, with failures. It
**does not establish dependable overall planning or autonomous visual quality**.

## Direct answers

- **Did planning work?** Entry, plan initialization, saving, revision, approval and
  approved implementation worked. Planning judgment remained uneven. Sol grounded
  its later plans and consulted on consequential choices. Luna sometimes deferred
  source questions or offered an approval plan before settling the strategy.
- **Did planning generate diagrams?** No. None of the eight planning sessions
  produced one, including the complex async case. Diagrams are optional; these
  runs do not establish natural diagram use during planning.
- **Did an ordinary explanation lead to a diagram?** Yes, in one completed Sol
  trial. The user only asked how Click is organized and how nested dispatch and
  invalid input work. Sol independently discovered the diagram/catalog/review
  guidance, rendered a native A2UI sequence, executed a probe against the actual
  checkout and revised/reviewed captures. The other six completed explanation
  sessions used prose, tables or ordinary example code. Luna never chose a diagram
  without being asked in this sample.

These are small, non-random, repeated trials across changing implementation
versions. Counts describe observations, not a model reliability estimate. Prose
was proportionate for the narrow comparison control.

## Exact initial requests

Each request below was sent individually through the real web composer. No user
request named a skill, tool, diagram family, component or implementation steps.
The architecture request explicitly asked for diagrams; the explanations did not.

| Case | Verbatim request |
| --- | --- |
| Callback explanation | Explain how Click turns command-line input into a callback invocation. |
| Small plan | Plan typo suggestions for misspelled subcommand names in Click. |
| Complex plan | Plan support for asynchronous command callbacks in Click while preserving existing synchronous behavior. |
| Explicit diagrams | Clone https://github.com/pallets/click and explain its architecture with diagrams. |
| Broader explanation | Explain how Click is organized and trace a nested command invocation, including what happens when the input is invalid. |
| Narrow control | Explain the difference between Click's Command and Context. |
| Ambiguous goal | Plan improvements to Click's command dispatch reliability. |

Normal product feedback, also retained verbatim:

- Both small plans: “Keep this to at most one suggestion. Unknown commands must
  still fail, and a suggested command must never run automatically. Leave
  successful command dispatch unchanged. Revise the plan before proceeding.”
- Sol's async consultation: “Use explicit opt-in async support and leave existing
  synchronous entry points unchanged.”
- Luna's async plan rejection: “Use explicit opt-in async support. Keep current
  synchronous entry points unchanged. Revise the plan around that choice before
  proceeding.”

The product's generated plan-revision/approval continuation contains internal
bookkeeping instructions. Those are preserved as product behavior, separately
from the operator's short feedback; they were not custom benchmark coaching.

## Environment and intervention

- Real UI on `http://127.0.0.1:5226`, connected to real backend `8221` and a private
  Core daemon. No model, network, catalog or visual response interception.
- UI commit `892a5e0e` throughout. Baseline backend `ec6554db9`; inspection cohort
  `a31161ba7`; explicit context bounds `f1ca07e7f`; individual-file search
  `8e86e467a`. Each session record identifies its version.
- Installed Base Agent was actually updated through the normal lifecycle API to
  **0.2.9**, marketplace `437b978`. The source change alone had left the installed
  copy stale; its declared tools were checked before post-fix submissions.
- Initial messages explicitly selected **high** reasoning for both
  `codex/gpt-6.1-sol` and `codex/gpt-6-luna`. Native message metadata verifies all
  19 selections and exact requests. Continuations used normal product behavior;
  they are not represented as independently selected high-effort trials.
- Separate fresh workspaces. All preseeded repos were unchanged real checkouts at
  `2247b35ea1c47c727d7a06e51fa280e12a863ff6`, version `8.6.0.dev`.
  Clone-request workspaces started empty; all three agents cloned the actual
  public repository at that same commit. Tracked-file hashes and git status were
  checked. No extra instructions or answer diagrams were put into the clones.
- Async cases started with the UI's Plan setting; other cases started in Execute.
  High-level planning requests discovered the planning skill and entered Plan.
- Ask-first remained active. The operator reviewed clone/read/approved test-edit
  permissions and granted scoped session permissions through the UI. The small
  plans were revised and approved in Interactive mode. No source implementation
  was permitted before plan approval. The async feature itself was not implemented.
- Existing preview `5219/8209` and its Core stayed running. Owned backend restarts
  retained private Core and all failed histories. No homepage change, merge or release.

## Reviewed outcomes

| Cohort | Sol | Luna |
| --- | --- | --- |
| Callback explanation | Inspected source; accurate prose/table/code, no diagram. | General explanation, no tools or diagram. |
| Original small plan | Saved plan and handed off; initial grounding left important dispatch/coverage facts unresolved and guessed five paths. Revision and Interactive approval worked; implemented the agreed single-suggestion change. | Saved plan and handed off, but missed existing implementation during initial planning; one guessed read failed. Revision and Interactive approval worked; implemented the agreed change. |
| Small plan after inspection tools | Correctly identified existing suggestions **and existing singular/plural tests**, proposed stronger assertions rather than duplicate implementation. Two hidden context-limit rejections recovered. | Identified existing implementation, but wrongly treated missing internal-symbol matches as missing behavior coverage. One hidden context-limit rejection recovered. |
| Async plan | Grounded strategy, consulted before detailed plan; after the product choice, covered wrappers, task-local contexts, result composition, cancellation, testing and compatibility. One guessed typing file failed. Consultation was prose, not the structured question tool. | Initially handed over a plan with the key async API/loop decision unresolved. After normal rejection feedback, produced a concrete additive-API plan with explicit limits. Zero failed tools, but initial strategy consultation did not occur. |
| Ambiguous reliability goal | Inspected dispatch/tests, distinguished potential gaps from confirmed defects, offered concrete scope choices and paused before drafting. Again used prose rather than a structured clarification question. | Avoided inventing a bug, but offered a generic audit-and-tests approval plan without a prior scope consultation. Safe source handling does not qualify intended-priority selection. |
| Explicit architecture, baseline | October 18 result remains in the historical report, excluded from these 19 sessions. | Two native diagrams, but incorrect parser-to-types edge, error/help branches after callback, tiny labels, catalog/layout refusals and a recovered capture storage refusal. Rejected as a clean semantic/visual pass. |
| Explicit architecture, fresh | Three native diagrams: module map, class model and successful-leaf sequence. Recovered a syntax error/capture timeout; revised and recaptured. Final 790 px native report inspected: readable and source-grounded with explicit scope captions. Useful final result after recovery. | Two native diagrams; corrected conversion/error-path semantics and simplified after captures. The long invocation diagram still has tiny labels and omits group-callback ordering from its simplified overview. One catalog-alias refusal. Partial visual/semantic result. |
| Broader plain explanation | First attempt stopped after two directory listings on a provider connection drop; retained. Fresh same-prompt retry independently produced native A2UI, verified the checkout with an executable probe, revised and captured twice. | First attempt rejected five file-targeted searches and fell back to shell; final prose was useful. Fresh same-prompt trial after the file-search fix completed 13 tools with **zero failures**, six actual file-targeted searches and no shell fallback. Prose, no diagram. |
| Narrow explanation control | Correct concise table/code explanation; no tools, Plan or diagram. | Correct short explanation; no tools, Plan or diagram. |

All planning source trees stayed unchanged before approval. The two approved
small-plan implementations changed four files each. Independent focused checks
returned **19 passed** for Sol and **14 passed** for Luna. The agents' broader
Click suites included 108 platform/feature skips and one expected failure; Luna
also reported seven typing errors in untouched files. These are not clean
full-suite/type-check passes. Cache permission failures and recovered test/style
errors remain in the histories.

## The naturally chosen diagram

[Open the actual Sol conversation](http://127.0.0.1:5226/workspaces/ws_adeb4df90a4a/sessions/sess_54e875c55ace).

The model authored an explicitly illustrative nested CLI, not a supplied answer
implementation. Its probe printed the imported module path under the fresh
workspace's `click/src/click/__init__.py`. The successful process exercised seven
standalone cases plus non-standalone `BadParameter` propagation. The callback and
cleanup events confirm that parent callbacks precede failed child validation.
The final diagram's alternatives agree with those events and exclude leaf
callback execution on invalid input. The source checkout stayed clean.

The first probe execution failed writing the sandbox's uv cache. The model
reran with workspace-owned cache storage and obtained the actual results.
Expected `Error:` output from asserted invalid-input cases is not a second probe
failure. This distinction was checked against stdout, stderr and exit codes,
not inferred from green tool rows.

Two genuine native PNGs were captured, with model revisions between them. The
final docked capture was independently opened and inspected. An operator browser
screenshot also confirms the retained surface reopens after finalization; its
fullscreen framing still requires vertical scrolling at 1280×720. Full responsive
presentation is not universally qualified. This source-grounded final result is
stronger evidence than a static renderer gallery, but the recovered failures and
one earlier provider interruption remain counted.

## Fixes and remaining limits

The new `fs_list_directory` and `fs_search_text` expose policy-checked read-only
inspection without opening shell in Plan Mode. Search accepts an actual file or
directory, bounded context, literal matches and explicit scan/output truncation.
Its bounds are declared in the tool schema. Common dependency directories and
links are not traversed. The existing file policy and source-write boundary remain.
Four capable marketplace roots declare the tools; specialist boundaries are retained.

The canonical planning skill now requires discovering actual paths, establishing
existing behavior and searching observable tests before proposing implementation.
The diagram workflow now checks exceptional paths and arrow meanings against
source, and requires reading the smallest labels in actual captured pixels.
These are generic contracts, not Click/model-specific answer instructions.

Remaining gaps include Luna's strategy consultation and coverage inference,
small labels after claimed visual review, catalog alias/canonical-ID confusion,
syntax/capture recovery, sandbox cache defaults and provider interruption. Sol's
explicit architecture case used 52 tools and about 1.9M input tokens; the natural
explanation used 37 tools and about 1.3M. Capability and efficiency are separate;
this work does not establish economical, consistent completion.

The ambiguity trials stop at consultation/approval. Unrelated-task plan reuse,
all dispatch policies, every diagram family and every responsive layout were not
benchmarked here. A clean tool sequence alone is not a semantic pass.

## Reproducible evidence and checks

[Machine-readable assessment and every session URL](planning-agent-outcomes-2026-10-19.json).

Retained local evidence: `D:/CLIO-Develop/planning-outcomes-20261019/`.
Audit archive: `planning-agent-evidence-20261019.zip` in that directory, 134
verified members, SHA-256 `42e1ae0e5a0954f1119ba1a4299876e2d9006262723924d6cf99e7b27bad1163`.
It contains predeclared criteria, per-session exact requests/versions/baselines,
whole native exports, permissions/questions, semantic audits, source diffs,
reviewed plan versions, real capture receipts and SHA-256 manifest. Original
failures and the unsubmitted superseded setup are explicitly distinguished.
The audit bundle is not a qualification of portable artifact-inclusive HTML replay.
The collector sorts newest-first API message results before checking prompts;
it preserves the unmodified native export bytes separately from projections.

Review covers tool arguments/results, native turn errors, shell stdout/stderr,
exit status, partial refusals, source/plan semantics, approval transitions and
actual final pixels. Automated diagnostic flags are triaged; expected probe
errors are not silently relabeled as failed tool executions or successful tasks.

Current focused validation:

- **69 backend tests passed** across real FS policy/bounds/MCP, plan lifecycle and
  read-only mode guards. No skipped tests in these focused checks.
- **127 top-level marketplace policy tests passed**, including capable-root tool
  declarations and specialist/superset contracts. After the CI review below,
  the paired nested pack checks also passed: **214 total** (164 policy/A2UI/Factorio
  checks plus 50 APPL Core checks).
- Targeted Pyright, Ruff lint/format and source file-size ratchet passed.
- These checks support the changes. They do not replace the live outcomes above.

### Post-assessment CI review

Hosted checks caught four mypy errors in the new search helper that targeted
Pyright had not caught. The helper now uses portable stat-mode checks instead of
Path predicates unsupported by the minimum Python typeshed, and explicitly types
its JSON context entries. Python 3.12-targeted mypy and all 17 real FS inspection
tests pass. Full **Linux-targeted mypy passes all 1,254 source files**. Full mypy
on Windows still reports nine POSIX-stub errors in four untouched infrastructure
modules; this is not an all-platform type-check pass.

The first 127-test marketplace command covered the top-level policy directory,
not the nested pack checks. Those hosted jobs validate against `develop`, which
has version beta 8 but not these unmerged FS tools. Their unknown-tool failures
correctly expose the dependency. The four updated roots now require beta 8 rather
than beta 3. Their real paired validation passes: 164 policy/A2UI/Factorio tests
and 50 APPL Core tests (the latter with its declared trimesh test dependency).
The workflow was not weakened or redirected to manufacture a green check;
marketplace hosted validation still requires backend #1800 in `develop`.

The local editable package metadata was refreshed from stale beta 7.post2 to the
source's beta 8. The visible test service was refreshed with the compatibility
fix and the versioned agent metadata through the normal update API; Base Agent
is enabled with no validation errors. The 19 model runs remain attributed to
their original commits, not retroactively relabeled as fresh model validation of
these typing/dependency corrections. Their original audit archive is unchanged.

Implementation remains in draft backend [#1800](https://github.com/iowarp/clio-agent/pull/1800)
and marketplace [#106](https://github.com/iowarp/clio-agent-marketplace/pull/106),
with the existing schema dependency [#39](https://github.com/iowarp/clio-schemas/pull/39).
