---
id: clio.compaction
title: Compaction checkpoint
description: What the model is asked to write when the context window fills; sent as the last message of the agent's own request, so editing it never costs the cached prefix.
x_kind: template
x_variables: focus, files
---
The context window is nearly full, so the work so far is being compacted. Write the checkpoint that replaces the history above: the work continues from it with no other memory of that history. Reply with the checkpoint as text; no tool runs in this reply.

What comes back verbatim after your checkpoint, so you need not restate it: the system instructions, the messages the person typed (the newest ones, as many as fit), the newest step of work, and the session plan, todo checklist and session facts when they exist. Record what happened against them instead. If the history starts with an earlier checkpoint, update it with what happened since rather than summarizing it again.

Use these sections, in this order, each a short list of bullets (write "none" when a section has nothing):

## Objective
What the person asked for, quoted from their messages, with the latest request last.

## Requirements and constraints
Every limit, preference, unit, exclusion or shape of answer the person set, quoted, and whether a later message changed it.

## Progress and state
What has been done, what is in progress, and the state of data, files and running work.

## Findings and evidence
- Keep exact file paths, dataset names, column names, variable names, units, dimensions, counts, statistics, artifact paths and error messages when they appear in the history.
- Say which source each finding came from, grouped by file, provider or workflow stage.
- If evidence is missing or a source was not inspected, say so. Do not fill gaps with plausible details or invent names, columns, statistics or conclusions the history does not support.

## Decisions and ruled out
Choices made and why; approaches tried that failed or were rejected, and why.

## Files and artifacts
Files read, written or created, and artifacts produced, with paths.

## Open threads
Unresolved questions, failed inspections, missing dependencies and checks not yet made.

## Next step
The next action toward the latest request, with a short quote of that request.{{focus}}{{files}}
