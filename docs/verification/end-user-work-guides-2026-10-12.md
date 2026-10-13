# End-user questions and work guides

Date: 12 October 2026. Website source: `site/` in the current checkout.
Initial guides: merged into develop through PR #1724. Visual follow-up: local
changes on `codex/visual-work-guides`, based on develop `0b0e7e25`. The follow-up
has not been published to the website.

## Reader coverage

Four linked guides are added under Use CLIO:

- `questions.mdx`: text, single choice, confirmation, connected-tool multiple
  selection and forms, URL consent, visual A2UI enrichment, independent open
  questions, answering ownership, and waiting versus independent work.
- `following-work.mdx`: tools, tasks, child agents, todos, command/output,
  cancellation versus waiting, and the difference between a terminal status and
  evidence that an assignment succeeded.
- `injections.mdx`: recorded context additions, every named UI label plus
  humanized tool-use guidance, and examples of task results and scheduled wakes.
- `workspace-panels.mdx`: compact Data/Work, full Work, Observability's
  Evidence/Timeline/Gantt/Context, opened content tabs, and remembered disclosures.

Sessions, widgets/interaction, evidence, and long-running work link to the new
pages. The previous sessions statement that nothing could continue until a
question was answered is corrected. No runtime or UI behavior is changed by
this documentation work.

## Contract checked against implementation

`ask_user_tool.py` currently defaults to `response_mode="blocking"`. Its native
kinds are freeform, choice, and confirmation. Async mode publishes a durable
question immediately and returns its identity; answers use the ordinary queue.
An attached A2UI surface is a presentation/input dimension, not an additional
native kind. Only the declared answer action submits its structured answer.

`interaction_types.py`, `elicitation_schema.py`, `routes/interactions.py`, and
the production question response components provide the connected-tool forms,
multiple selection, and URL consent distinctions. Permission review, plan
review, and draft selection retain their own controls.

The proposed contract where every ask returns a durable handle immediately and
the agent explicitly waits on that question is **not implemented in this
checkout**. `wait_tasks` accepts actual task handles, not question handles.
The public guides describe existing modes and do not promise the proposed API.

UI source reviewed: `conversation-harness-injection.tsx`, `tool-invocation.tsx`,
`tool-terminal-result.tsx`, `open-questions-tracker.tsx`, `open-question-list.tsx`,
`session-evidence-popover.tsx`, `session-work.tsx`, `observability-dock.tsx`,
`context-canvas-panel.tsx`, and `workbench-shared.ts`.

## Media and evidence limits

The original seven images were insufficient: two pages had one image each,
the first images followed long tables, and the visual-question illustration was
only a thin gallery control. The follow-up replaces that layout with seventeen
placed images: sixteen new captures and the retained conversation-family Gantt.
Each page begins with a useful product frame before its reference tables.

The new captures show a native confirmation, a drafted text answer, a complete
chart-enriched choice question with optional comment, three open questions,
collapsed and expanded command details, a collected unbounded task wait,
current and completed todos, grouped injections, a goal judgment, compact Work,
Evidence navigation, the full Work inventory, and a report opened in its tab.

The fictional bike-station workspace, artifacts, dashboard, A2UI surface, three
async questions and five todos were authored through production tools in an
isolated service. The chart is attached to a real durable question. Drafts were
entered through actual response controls, but no answer was submitted. This
does not claim that a model resumed after submission or that the proposed
always-async question contract is implemented.

Commands, waits and injections replay authentic retained public-example
records. Their input, output, instructions and timing are preserved. The
`wait_tasks` record has `return_when="all"`, `timeout_s=null`, one completed
subagent result, no pending tasks, and `timed_out=false`. The replay did not
execute the command or delegate another agent. The loop collection uses the
loop wake and shell call from the same recorded turn.

No model inference, OPAL run, Aurora infrastructure test or private-session
import was performed. These are documentation stills, not continuous video or
fresh model/runtime qualification. Only framing crops were applied; no controls,
labels, answers or result pixels were painted or composited.

Archive:
`D:/Libraries/Videos/clio_recordings/2026-10-12-user-work-guides/`.
The source records and display projections are retained separately from the
images. Rejected technical-dialog framing and extra gallery space remain in
`rejected/`. The site capture manifest records SHA-256 checksums and source
identity, including dirty source-file hashes rather than relying on HEAD alone.

Follow-up archive:
`D:/Libraries/Videos/clio_recordings/2026-10-12-work-guides-visual-expansion/`.
`work-guides-visual-expansion-manifest.json` records the sixteen assets, hashes,
source heads and crops. The archive keeps the isolated authoring script, real
question/work/artifact responses, original stills, contextual frames, recorded
source excerpts, display projections and browser-review evidence.

## Initial review

- `pnpm check`: passed, zero errors/warnings and six existing hints.
- `pnpm build`: passed, 85 pages, all internal links valid. The bundler reports
  module-directive warnings; they are not claimed as a warning-free build.
- Production preview on port 4323: all four guides reviewed at 1440 and 390 CSS
  pixels. No document-width overflow. All seven screenshots loaded and enlarged
  through the actual viewer. Dark question detail and light Work detail inspected.
- Screenshot captions, ordinary-language prompts, gallery links, sidebar entries,
  and updated crosslinks checked. No new middot separators.
- Test suites were not run, following the user's earlier instruction.

## Visual follow-up review

- `pnpm check`: passed, zero errors/warnings and six existing hints.
- `pnpm build`: passed, 85 pages and all internal links valid; existing module
  directive warnings remain.
- Production preview on port 4323: seventeen images across all four guides loaded
  and opened in the full-size viewer. All four guides were reviewed at 1440 and
  390 CSS pixels, with no document-width overflow or browser page errors.
- Rendered review found that the capture helper offered only 480px for source
  images between 480px and 800px. It now includes a native-size candidate below
  the existing 1246px cap. The Work and question-list captures use their available
  resolution rather than enlarging the 480px version.
- Build output, responsive image checks and rendered desktop/mobile evidence are
  retained in the follow-up archive.
- No test suite was run, following the user's earlier instruction.

Publication still follows the website's main-branch release path. This work
does not establish that main, the installed product, or the public site contains
the visual follow-up. At capture review time, develop contained the original
guides; this revision and the local preview contain the additional media above.
