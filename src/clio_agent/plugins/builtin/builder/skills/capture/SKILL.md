---
name: capture
description: Turn work from a session into a plugin the person can reuse, or make an existing plugin's next version from the sessions that used it. Use for /capture and /optimize, and when the person says it in their own words - "make this reusable", "I want to do this again next week", "teach CLIO this", "save this as a tool", "next time do it this way".
---

# Capture: from a session to a plugin

The person never writes a file and never has to know which kind of part their knowledge
became. You read the session, ask only what the record cannot answer, propose the
parts, write and check them, have them reviewed, try them, and install them in the
person's private marketplace. Every stage is a conversation with the person: go one
stage at a time and wait for them where a stage asks.

For `/optimize` (a plugin named, or none), read `optimize.md` in this skill first: it
is this same procedure with a different starting point.

## 1. Read the session

Call `session_record` (no arguments for this conversation; the ids of sessions the
person named with `@`). It writes each session's full record, children and flows
included, to a file and returns the paths with a summary. Read the files in
`run_code`, never into the conversation: load the JSON and pull out

- which tools ran and in what order (`tool_records`), and what each produced;
- `run_code` written more than once for the same computation;
- the corrections and refusals the person gave, and the questions you asked them;
- the subagents and flows that ran (`children`), the connections and sources used;
- what was produced (files, figures, answers).

If the session used a plugin the person owns (its `.agent.json` lists the plugins its
agent carried; a `private-local` one is theirs), say so and offer `/optimize` for that
plugin before proposing a new one.

## 2. Say what you found

Before any question, show the procedure you saw as a `clio.steps.v1` view (one step per
thing that was done, in order), then two short lists: what looks general (would be
done the same way next time) and what looks like a one-off. Ask questions only for
what the record cannot answer.

## 3. Separate constants from variables

For every concrete value in the procedure (a path, a dataset, a threshold, a station
or sample id, a unit, a model), ask whether it is fixed for this lab or changes per
run. Group them into one question with a `ChoicePicker` per value or a short list when
there are many. Fixed values go into the plugin's text. Changing values become a
skill's inputs, a flow's options or a command's arguments. Do not decide this alone:
it is the step most likely to be wrong.

## 4. Propose the parts

Show a plan: one row per part with what it does in the person's words and why it is
that kind of part, and below it the kinds you chose not to use and why. Use a
`clio.mermaid.v1` diagram when the parts call each other. The mapping:

| What the session shows | Part | Reference |
| --- | --- | --- |
| A procedure the person walked the agent through | Skill | `references/skill.md` |
| Something the agent could not infer and needed every time (a data layout, a naming rule) | Prompt fragment within ~150 tokens; a skill reference when longer | `references/fragment.md` |
| A correction that must never be violated ("never write to raw/") | Hook that denies with a reason | `references/hook.md` |
| The same computation rewritten in `run_code` | Tool (`@tool`), or a script binding when scripts call it | `references/tool.md` |
| A fixed sequence with fan-out, checks or loops | Flow, or a workflow when the steps can be listed as data | `references/flow.md` |
| Work handed to a specialist with its own instructions | Subagent | `references/subagent.md` |
| An external service reached through a saved connection | Named connection | `references/connection.md` |
| A way the person wants to start the whole thing | Command | `references/command.md` |
| The set of plugins the person works with | Agent (**Save as agent**) | `references/agent.md` |
| A picture or view the person keeps asking for | Existing components; an MCP App when they cannot show it; a new built-in view only through a pull request to CLIO | `references/view.md` |

- Never write an MCP server for something a `@tool` does, and never one for a service
  that already has a saved connection.
- Require installed plugins (`workspace`, `hdf5`, `slurm`...) instead of copying their
  parts.
- Facts about the person (their name, their preferences in general) are not a plugin:
  they belong to long-term memory. Say so if the session has them.

Write nothing until the person approves the plan.

## 5. Detail each part with the person

Load the reference of each part you will write
(`load_skill("capture", files=["references/skill.md", "references/hook.md"])`). For
each part ask only the questions that change the file: when should this skill be used
(its description is its trigger, so use the person's words), what should the hook say
when it refuses, which steps of the flow need the person's approval. One part at a time.

## 6. Write and check

Call `make_plugin(name, files)` with every file. Pick a short lowercase name for the
person's field and procedure. It checks the plugin as `clio-agent plugin check` does:
when the check fails, fix the files yourself and call it again; do not show the person
a diagnostic. When the name is refused:

- `name_taken_by_person`: the person already has a plugin by that name; pick another
  name, or for a change to their plugin write a variation (`variation_of`).
- `name_taken_by_marketplace`: another marketplace has it; write the person's variation
  under a new name with `variation_of="<marketplace>/<plugin>"`. The original is never
  edited.

## 7. Review

Start the `capture-reviewer` subagent with the plugin's name and every file's text, and
not the session. It reports what a stranger would get wrong. Fix what it reports with
`make_plugin` again (same name: a new version), and tell the person in one line what
changed because of the review.

## 8. Try it

Offer a trial; a person who says "skip it" skips it. Take one or two tasks from the
session and restate each as a new person would ask it, without the answers. Run each
twice as children, together (`spawn_agents_parallel` with `wait=true`):

- once with the plugin: `{"task": ..., "name": "with <plugin>", "plugins": {"add": ["private-local/<plugin>"]}}`
- once without: `{"task": ..., "name": "without <plugin>"}`

A plugin with executable parts is asked about once here (the person sees its review).
Show the two answers side by side (a `Tabs` or two columns) and ask which is right.
If the plugin loses, ask why, fix it and offer the trial again. Without spawn tools in
this agent, say the trial needs the `delegation` plugin and skip it.

## 9. Hand over

The plugin is in the person's private marketplace. Add it to this conversation with
`offer_plugin(plugin="<name>", reason=...)`. When the person's agent plus this plugin is
what they will start from, offer **Save as agent** (see `references/agent.md`). End with
one line on how to change it later: run `/optimize <name>` after using it, or `/capture`
again on a session that used it.
