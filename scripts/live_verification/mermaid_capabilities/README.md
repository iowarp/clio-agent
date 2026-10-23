# Mermaid capability trials

These trials test the real CLIO agent and renderer. They do not substitute
hand-authored diagram source, intercepted API responses or gallery screenshots.
`mission.txt` supplies six independent subjects; `holdout.txt` supplies eight
different subjects, including three grammar notations. Neither supplies an
answer document. Keep prompts and semantic rubrics fixed before comparing runs.

These are directed capability exercises: the prompts explicitly request Mermaid,
component guidance, a saved report and image review. They do **not** establish
that an ordinary user request discovers those capabilities or completes the
workflow without coaching. Preserve those exact prompts when reporting results.

Use an isolated real service/Core namespace and a matching production UI.
Keep existing user services and workspaces intact. Configure the real provider
normally; never put tokens in evidence or these scripts. Use fresh workspaces
for every stage/model. The prepared session selects its own model.

1. Run `submit.py --base-url URL prepare --output EVIDENCE --stage STAGE
   --model MODEL --mission PATH`. The script prints the workspace/session route.
2. Open that route in the actual UI. Confirm it is live before starting work;
   an unmounted client cannot qualify visual review. Choose and record the
   docked/expanded viewport. Do not change source files for the model.
3. Run `submit.py --base-url URL start RECORD.json`. Approve only the scoped
   test-file operations necessary for the trial; retain those approvals.
4. Let the turn finish. Preserve errors and incomplete reviews. Any follow-up,
   operator source repair or viewport intervention is a separate phase, not
   an unassisted first-turn pass.
5. Run `collect.py EVIDENCE --traces SEMANTIC_TRACES`. This retains raw model
   tool observations and validates the exact capture files against their hashes.
   Posthoc transcript merging is not the primary evidence for repeated calls.

All Python commands use `uv run`. Run the separate documentation check with:

```text
node scripts/live_verification/mermaid_capabilities/verify-guide.mjs
  --ui-root UI_CHECKOUT --catalog CANONICAL_WORKSPACE_CATALOG --out RECEIPT.json
```

That check compares the complete family inventory and renderer version, then
parses/renders every local recipe. It does not qualify model interpretation or
visual quality.

## Grading

Record tool counts, catalog index/deep lookup counts, loaded-content hashes,
parser/render failures, human interventions and the model's own review claims.
Count the catalog index and one batched component lookup separately from the
workflow/review skill loads. Do not turn one success into a reliability estimate.

For `mission.txt`, score each subject independently: all lifecycle transitions
and terminal completion; ER attributes/optionality/cardinality at both ends;
exact commit parents and merge; complete grammar with nonempty identifiers,
comma repetition and optional filter; mutually exclusive protocol branches and
ordered messages; every tree parent and file. A valid parse alone is insufficient.

For `holdout.txt`, check inheritance versus composition and multiplicity;
responsibility lanes and approval/rejection paths; dates/durations/dependencies;
one deployment host with the stated containers/technologies; inclusive packet
widths; shared timeline periods; verification direction/risk/method; equivalence
of the three accepted grammar languages. Check the parsed model as well as source.

Grade rendered review separately: matching artifact/version and tab/control
state, actual image inspected, readable text/arrows at the stated width, defect
identified, correction captured, all relevant views covered. An unavailable
viewer is a setup gap; a blank view from a bad tab ID is an authored defect.
Honest reporting of an incomplete review is valuable but is not a visual pass.

## Ordinary-request follow-up

On October 18, 2026, fresh Sol and Luna sessions received this exact request
through the connected web UI:

> Clone https://github.com/pallets/click and explain its architecture with diagrams.

Both cloned the real repository and independently loaded the diagram guidance.
Sol inspected implementation files, authored three A2UI diagrams, revised them
and obtained two real captures. The final narrow sequence view is still small;
capture success alone is not a readability pass. Luna returned two raw Mermaid
fences without creating an A2UI surface or capturing a rendered result. That is
a failed rendered-diagram outcome. The skill and component annotation previously
suggested fences for short explanations; both now distinguish a requested visual
from a request for source code. A fresh behavioral retest is still required.

Two more fresh sessions received:

> Plan typo suggestions for misspelled subcommand names in Click.

Each had an unchanged real Click checkout. Both independently entered Plan mode
and left tracked source unchanged. Both failed the normal saved-plan approval
handoff. Entering via a skill during a turn did not run plan-path initialization;
Luna's first proposal hit a missing directory, and Sol's first exit hit a missing
plan file. Sol later saved a plan but did not retry the handoff. Luna asked for a
repository location already supplied by the workspace. Shell inspection was also
blocked by the deliberate structural Plan ACL, contradicting the skill's old
read-only-shell promise. Do not label these as planning passes.

Two async-planning UI submissions were attempted before the private test service
became unavailable; their model outcomes are unconfirmed. Ambiguity, revision,
implementation approval and execute-mode controls remain unqualified. Automatic
approval review rejected restarting the private test backend/UI with only
“blocked by policy” as the stated reason. The extensive benchmark is incomplete;
the completed failures are retained rather than replaced by coached reruns.
