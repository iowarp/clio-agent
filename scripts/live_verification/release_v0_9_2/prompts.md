# Exact live prompts

Use fresh sessions. Preserve the returned workspace/session IDs in the runtime
manifest. Do not substitute historical session output.

## Progressive loading and compaction

1. `Remember this exact early marker: V092-EARLY-CEDAR. Reply with the marker and one short sentence.`
2. Patch only this session to `automatic_compaction=true` and
   `autocompact_pct=0.01`, then send:
   `Remember this exact middle marker: V092-MIDDLE-ORBIT. Explain in three short paragraphs why transcript continuity matters.`
3. After the automatic checkpoint is observed, restore `autocompact_pct=0.85`.
4. Perform manual compaction, send:
   `Remember this exact late marker: V092-LATE-EMBER. Reply with the marker and one short sentence.`
5. Perform the second manual compaction and restart the backend without deleting
   its CTE/state directory.
6. After Browser reload:
   `State the early, middle, and late markers in chronological order, then explain which one was introduced after the first manual compaction.`

## EarthScope Skills

1. `Find the five EarthScope GNSS stations nearest Palm Springs, California. Show the bounded ranked candidates on the interactive station map, then stop and wait for me to choose. Do not stage a station time series yet.`
2. In Browser, select a non-leading returned station and submit the surface action.
3. `Continue with the station selected in the interactive surface. Stage its time series, profile the exact columns, and show east, north, and up on the interactive time-series chart. Also export a PNG and write a concise Markdown report that records the selected station, observed provenance, plotted scope, and limitations.`

## Factorio Flat and Daisy specialists

Attach `fixtures/materials_scan_speed_fatigue.csv`, then send:

`Using only the attached synthetic qualification dataset, assess whether laser scan speed is associated with fatigue-life changes without claiming causality. Consult the materials scientist, manufacturing expert, characterization expert, mechanical-testing expert, fatigue/failure expert, and data-analysis expert. Start independent consultations in parallel; observe their evidence, wait for terminal outcomes, and collect each result. Address porosity, build orientation, run-outs, the missing roughness value, uncertainty, and the additional evidence needed for a causal claim. Produce one concise Markdown validation report and one figure.`

## Tool presentation

Use the exact bounded prompts in `../tool_presentation/prompts.md`, but consolidate
them into fresh sessions by blueprint. Cover each row in `coverage.md`; do not use
the historical verdicts. For the workflow session, explicitly ask:

`Load the inspect-qualification-brief skill, open its exact SKILL.md in the side panel, run the declared workflow once, and show the workflow definition and hierarchical execution graph.`

For cross-file triage, explicitly require parallel children followed by patterned
Observe, Message, Wait, and Collect operations.

## MCP App

`Call the v2ex_ui_echo tool exactly once with payload='release-v0.9.2-ui-probe'. Do not call another tool. Tell me when the interactive result is ready.`

In Browser, open the app, verify `Result for release-v0.9.2-ui-probe`, click
`Continue with this result`, confirm the same session resumes, reload, and reopen
the app.
