# Mermaid component guidance and model benchmark

The October 18 ordinary-request follow-up below is a historical snapshot. The resumed October 19 trials, exact short prompts and current limitations are recorded in [Planning and natural diagram outcomes](planning-agent-outcomes-2026-10-19.md). The earlier incomplete or directed runs remain retained.

The component entry is now the syntax/capability authority. The benchmark
demonstrates model authoring across substantially more than sequences, but it
does **not** establish universally reliable autonomous visual review.

## Guidance ownership

| Layer | Responsibility |
| --- | --- |
| Main and planning prompts | Offer diagrams, HTML/SVG and data views when useful; route to the declared component guidance without requiring a diagram or layout. |
| Catalog index | Short Mermaid description/signature and an exact deep lookup. |
| `clio.mermaid.v1` component lookup | Complete pinned inventory of 33 families, syntax, examples, notation variants, source/artifact inputs, viewer controls, navigation, theming/sizing, limitations and recovery. |
| `create-diagram` | Choosing a notation, preserving evidence, composition and review. The two competing syntax references were removed. |
| `review-visual-presentation` | Inspect the actual published pixels, match artifact/control state, revise and recapture. |

The canonical schema carries the guide in `x-clio-authoring`, a schema
annotation returned by the existing component lookup. It is not a component
payload property. The generated index stays short and the wire contract is
unchanged. The guide supplies 41 runnable recipes, covering all 33 installed
Pele 1.6.0 families, four railroad notations and five C4 levels. A verification
script compares the complete installed family inventory and version rather
than maintaining a second guessed list.

Shared prompt routing was updated in the existing Base, APPL Core, EarthScope
and Factorio roots. Existing automatic presentation-skill discovery remains in
place for eligible producers; specialist capability boundaries are preserved.

## Method and evidence

Evidence directory: `D:/CLIO-Develop/mermaid-capabilities-20261018/`.
The committed harness and prompts are under
`scripts/live_verification/mermaid_capabilities/`.

These are real CLIO sessions using `codex/gpt-6.1-sol` and `codex/gpt-6-luna`,
separate workspaces and the actual client/server. The notes are explicitly
synthetic. No answer diagrams were supplied, no model responses were replaced,
and no gallery route or intercepted catalog response qualified a model run.
Scoped test-file approvals are operator interventions, not authored solutions.

The original prompts explicitly require Mermaid, component guidance, publication
and image review. They are directed capability exercises, not evidence that an
ordinary request discovers and completes that workflow. Exact prompts remain in
`mission.txt` and `holdout.txt`; no answer diagrams were supplied, but the workflow
was supplied. The ordinary-request follow-up below tests that separate question.

`mission.txt` describes six independent subjects: lifecycle, ER model, branch
ancestry, grammar, request protocol and project hierarchy. `holdout.txt`
describes eight different subjects: classes/composition, responsibility lanes,
schedule, deployment, packet, chronology, requirements and three grammar
notations. Each model authored 16 diagrams across those 14 subjects. The
holdout source/model structures were inspected against the stated rubric;
successful parsing alone was not the semantic grade.

The collector reads the raw `react.step.completed` observations, keeps tool
and lookup counts by turn, and verifies each actual PNG against its recorded
SHA-256. `model-artifacts.json` retains authored source and parsed models;
`*-receipt.json` retains loaded guidance, tool observations, publications,
captures and their exact viewer states. Early failures remain in the record.

## Results

| Trial | Model | Finding |
| --- | --- | --- |
| Baseline six-subject set | Sol | Six diagram types authored. Seven skill lookups total; catalog index and component schemas were part of a longer reference chain. Actual captures and revisions occurred. |
| Baseline six-subject set | Luna | Six types authored, but its initial tab value named a nested diagram rather than a tab child. No visual pass. |
| Revised six-subject set | Both | Discovered `treeView-beta` from the complete component entry instead of falling back to a mindmap. Six diagrams per model parsed/rendered. Luna repeated the invalid-tab error. |
| Final six-subject set | Sol, `sess_4df3c7bb0229` | 26 tool calls, six skill lookups, 13 capture requests and 10 successful captures. Inspected all six subjects and revised headings/composition. The full-report capture exceeded the image limit; sectional captures were retained. Labels remain small at the 264 px docked width. |
| Final six-subject set | Luna, `sess_939b8eba22c2` | First turn: 17 calls, five skill lookups, two successful captures from three requests. Corrected ER endpoint markers, then stopped after two views. A separately recorded follow-up used 17 calls and four successful captures from six requests to inspect the remaining four. This is not an unassisted first-turn visual pass. |
| Eight-subject holdout | Both | Ten diagrams each parsed/rendered: class, swimlane, Gantt, C4 deployment, packet, timeline, requirement and three railroads. Seven subjects met the checked source/relationship rubric. Grammar-equivalence qualification failed the case-sensitivity check described below. No autonomous visual pass is claimed for these runs: their viewer/capture setup did not produce review images. |

The final primary runs used one catalog index lookup and one batched component
lookup. Other skill loads were workflow/review guidance, not additional Mermaid
syntax manuals. These are small, non-random trials, not a statistical estimate
of model reliability or a claim that live agents exercised all 33 families.

The prepared trials named `qualified` in their filenames ran before their
viewers were mounted. They are retained as setup failures and do not qualify
visual acceptance. The corrected harness separates prepare, mount and submit.

## Ordinary requests through the connected UI

Evidence: `D:/CLIO-Develop/planning-outcomes-20261018/QUALIFICATION.json` and the
four complete `*-receipt.json` files alongside it. Fresh sessions used the real
web composer and the same two models. The architecture request was exactly:

> Clone https://github.com/pallets/click and explain its architecture with diagrams.

Both agents independently loaded diagram guidance and cloned the real repository
at `2247b35ea1c47c727d7a06e51fa280e12a863ff6`.

| Trial | Finding |
| --- | --- |
| Architecture, Sol | Inspected implementation files and authored three A2UI diagrams. Two real captures and model revisions are retained. Operator review of the final 591 px image finds a small sequence view; expanded readability is unqualified. |
| Architecture, Luna | Returned two raw Mermaid fences. No A2UI creation or rendered capture. This fails the requested visual outcome despite successful skill discovery. |
| Small plan, Sol | Entered Plan, inspected files and left source unchanged. Initial exit failed before the assigned plan existed. Later saved a grounded partial plan but did not retry the approval handoff. |
| Small plan, Luna | Entered Plan and left source unchanged. Attempted exit before writing, hit the shell restriction, then a missing plan directory; asked for a repository location already present in its workspace. No normal approval handoff. |

The planning request was exactly:

> Plan typo suggestions for misspelled subcommand names in Click.

Each planning workspace contained an unchanged checkout of that same source,
with tracked-file hashes retained before and after. Neither plan is a complete
workflow pass. No implementation instructions, diagram types, skills or tool
names appeared in these user requests.

The raw-diagram failure exposed incorrect advice in both `create-diagram` and
the component annotation: they suggested fences for short explanations. Both now
request rendered A2UI for visual requests and reserve fences for source/Markdown
export. The planning runs exposed missing mid-turn plan-path initialization;
skill entry now uses the shared reminder/lifecycle after recording variant and
playbook metadata. The skill no longer promises read-only shell calls: the
structural Plan ACL deliberately blocks all shell execution. No shell exemption
or weakening of the mode boundary was added.

The fixes have 34 component-authoring checks, canonical export verification, 51
planning/skill-effect regression checks, Ruff and targeted Pyright. These are
regression results, not fresh model acceptance. Two async-planning UI submissions
have unconfirmed model outcomes after the private service became unavailable.
Automatic approval review rejected restarting backend 8221 and UI 5226, stating
only “blocked by policy”; the action was not retried through another mechanism.
Fresh post-fix model runs, ambiguous-task consultation, scope revision, approved
implementation and execute-mode controls remain unqualified. The extensive
planning benchmark is incomplete.

## Failures that changed the implementation

**Invalid tab selection.** The smaller model selected a diagram ID while the
tab's actual child was its surrounding Frame. The viewer opened without
selected content. The schema entry now gives the exact Frame/child example;
backend component validation rejects an invalid literal selection with the
allowed child IDs. Arbitrary data/context objects are not subjected to that
component relationship rule. Dynamic bindings remain supported.

**Repeated capture evidence.** The model received distinct fresh images, but
posthoc transcript merging matched repeated arguments to an earlier invocation.
The raw per-step records and separate image files established the difference.
The merger now refuses to pair a different known call ID with an already
identified invocation, preserving each image/result. Regression tests cover
both ID-less wrapper/observer pairing and distinct explicit IDs. The benchmark
uses raw observations independently of that display fix.

An early operator interpretation of escaped trace text incorrectly attributed
the blank view to newline encoding. Inspection of the saved source bytes
disproved that interpretation. No speculative newline-parser change is shipped.

**Schema fidelity.** Luna's final ER diagram repaired cardinality, but still
added unprovided keys, attribute types and a Calibration attribute. Relationship
correctness does not erase this fidelity failure. The component guide now
explicitly preserves unspecified types/keys/attributes. The separate fidelity
prompt tests a different schema and reference-backed source, rather than
copying the original example. Its session was prepared, but automatic approval
review rejected the command starting that extra trial without giving a detailed
reason. It is **not run**, and no post-fix model acceptance is claimed for that
guidance addition or the reference-backed trial.

**Grammar semantics.** Both holdout models used bare ABNF quoted literals.
Those are case-insensitive, unlike the equivalent EBNF/PEG literals; identical
rendered labels do not prove identical accepted languages. The guide now
explains this and demonstrates exact ASCII hex terminals. The installed parser
rejects RFC 7405 `%s`/`%i` prefixes, so those are documented as unsupported here.
The updated recipes render successfully. This clarification has not received
a fresh model trial; the earlier outputs remain failed equivalence evidence.
The standards distinction is documented in
[RFC 7405](https://www.rfc-editor.org/rfc/rfc7405.html).

## Validation boundaries

- The complete schema suite passed 859 tests. Canonical exports, Ruff and
  Pyright passed. Schema and marketplace hosted checks are recorded separately.
- All 41 recipes parsed/rendered through the actual installed dependency. The
  Ishikawa recipe additionally checks its intended hierarchy: an indentation
  parser can otherwise accept invented keywords as visible labels.
- Backend regression tests cover real component lookups, tab selection, capture
  evidence custody and repeated-call identity, alongside the existing suites.
  The focused combined run passed 169 tests with no skips; lint, formatting and
  targeted type checks passed. Existing deprecation warnings and Core nanobind
  shutdown diagnostics remain visible in the logs.
- Captured images are actual model artifacts. Readability at 264 px remains a
  limitation; expanded-view acceptance and all-family live-model coverage are
  not claimed. The benchmark contains failures and follow-ups, not only wins.

Follow-up branches target `develop`. No homepage changes, main merges,
package publications, tags or releases are included.
