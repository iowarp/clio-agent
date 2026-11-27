# Command

A way the person wants to start the whole thing: `/reduce-run run42.h5`. A command's
text becomes the person's message, with their arguments filled in, so the agent then
follows the plugin's skill or flow.

## Format

`ai.iowarp.clio/commands/<name>.md`; the file name is the command's name, and in a
conversation it is `/<name>` (or `/<plugin>:<name>` when two plugins share it):

```text
---
description: <what it does, shown in the / menu>
argument-hint: "<what to type after it>"
---
<the message the agent receives>
```

- `$ARGUMENTS` is everything typed after the command; `$1`, `$2`... are its words,
  quotes keep words together.
- Keep the body short and point at the skill or flow that does the work ("Load the
  `check-a-run` skill and check $1."): the procedure lives in one place.
- Commands are text parts: no trust step.

## What `plugin check` enforces

- A command whose frontmatter does not parse is reported with the file.
- Two plugins with the same command name are both listed under their full names; the
  short name then asks which one.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "run-checks",
  "version": "0.1.0",
  "description": "Start this lab's run check with one command.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```markdown ai.iowarp.clio/commands/check-run.md
---
description: Check a beamline run before analysis.
argument-hint: "<run file> [expected frames]"
---
Check the run $1 before I analyse it, the way this lab does: confirm the detector and
energy units and count the frames (expected: $2).
```
