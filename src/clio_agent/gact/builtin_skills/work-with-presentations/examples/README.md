# Build and review the presentation reference

The example contains six slides using fictional station summaries in arbitrary
units. Its native tables and chart stay editable; speaker notes retain the source
context and uncertainty. It is a design example, not a real experiment.

1. Prepare the execution runtime and confirm JavaScript is ready.
2. Load `create-reference-deck.mjs` through this skill's file argument and save it
   in the returned JavaScript workspace, where `pptxgenjs` resolves.
3. Execute it with the returned `javascript.script_argv`, passing a task-owned
   output directory. It writes `reference-deck.pptx` and `reference-source.json`.
4. Reopen the deck to check all six slides, tables, chart and notes. Call
   `prepare_document` with `action="render"` on it. Inspect the six-slide contact
   sheet, then choose individual slides for closer review when the overview shows
   unclear text or possible layout problems.
5. Revise the native builder, regenerate and repeat if needed. Publish the final
   editable deck with its source-bound PDF preview.

Use `references/pptxgenjs.md` for library mechanics and `references/slide-design.md`
for narrative and composition. Choose the slide count and layouts from the real
evidence and audience. Do not preserve fictional example findings in a user deck.
