# Skill

A procedure the person walked the agent through, written so the agent can follow it
next time without them. The agent sees only the skill's name and description until it
loads it, so the description decides whether it is ever used.

## Format

`skills/<name>/SKILL.md`, in the Agent Skills format:

- frontmatter `name`: lowercase letters, digits and single hyphens, up to 64
  characters, equal to the folder name;
- frontmatter `description`: 1 to 1024 characters. Say what it does and when to use it
  in the words the person used ("plot the beam profile", "check a run's units"), not
  the tool names;
- body: the procedure as numbered steps. Write the changing values as inputs ("the run
  file the person names", "the threshold, 0.5 unless they say otherwise"), never the
  one-off values from the session;
- files next to `SKILL.md` (references, small scripts, templates) travel with it and
  are read with `load_skill("<name>", file="...")` only when needed.

## What `plugin check` enforces

- `skill_invalid` (warning, the skill is skipped): a bad name, a name that differs from
  its folder, or a missing or too long description.
- `skill_collision` (error): two plugins in the same agent bring a skill of the same
  name. Choose a name specific to the person's field.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "beamline-checks",
  "version": "0.1.0",
  "description": "How this lab checks a beamline run before analysis.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```markdown skills/check-a-run/SKILL.md
---
name: check-a-run
description: Check a beamline run file before analysing it - open it, confirm the detector and the energy units, and flag missing frames. Use when the person brings a new run or asks whether a run is usable.
---

# Check a run

Inputs: the run file the person names; the expected frame count (ask if unknown).

1. Open the file and list its groups.
2. Confirm the detector name in `/entry/instrument/detector/name`.
3. Read the energy axis and its `units` attribute; this lab records energies in keV.
4. Count the frames and compare with the expected count; name any gap.
5. Report: usable or not, and why, in two sentences.
```
