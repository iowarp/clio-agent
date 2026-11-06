# The Prompt Registry: Reminders a Person Can Edit

Tracking issue: https://github.com/iowarp/clio-agent/issues/326
Design of the reminders themselves: [reminders.md](design/sdk/reminders.md)

> **Agent prompts are not part of this registry.** An agent's main prompt, and
> each defined subagent's, is composed from the prompt fragments of its
> resolved plugins in five fixed sections (identity, guidelines, rendering,
> capabilities, domain), each attributed to the plugin that owns it; see
> [PLUGINS_AND_FLOWS.md](PLUGINS_AND_FLOWS.md#prompt-fragments). Core's own
> fragments ship in the core plugin (`plugins/builtin/core/ai.iowarp.clio/prompt/`):
> `core.guidelines`, `core.rendering`, `core.tasks`, `core.plan` and `core.code`
> (on when `run_code` is available: batch many filtered tool calls into one script).
> The list of deferred tools, under "Tools you load when needed", is generated
> per request (`gact/agents/deferred_tools.py`), not a fragment.

## What the registry holds

`src/clio_agent/prompts.py` (`PromptRegistry`) holds the prompts a person can see
and change in Web or Desktop (Settings, Prompts, the **Reminders** tab). Its
built-in prompts are core's **injections**: everything CLIO adds to what an agent
reads beside the system prompt (the session's facts, the plan and the todos as
working state, plan mode, the stale-plan note, the explore note, compaction notices,
the cut-result reminders that point at a spilled result file, hook notices...). Each
is one Markdown file in `plugins/builtin/core/ai.iowarp.clio/injections/`, with the
id `clio.reminder.<name>`; `clio_agent.injections` resolves and renders them.

A plugin may declare its own injections in `ai.iowarp.clio/injections/*.md`. The
Reminders tab lists them beside core's (`clio.reminder.<plugin>.<name>`,
`clio_agent.injections.listing`), and a person switches them the same way.

The compaction prompt is not in the registry: it is part of CLIO core, next to the
compaction code (`gact/compaction_prompt.md`, placeholders `{{focus}}` and
`{{files}}`; `clio_agent.compaction_prompt`). The former `compaction.prompt_file`
setting and `CLIO_COMPACTION_PROMPT_FILE` are gone; a value still set fails the
compaction with the typed `compaction_prompt_invalid` error, so an old override is
never ignored silently.

## File format

A prompt file is Markdown with flat frontmatter (`key: value` lines, no nesting):

```md
---
id: clio.reminder.plan_stale
title: Mention a plan that has not been updated
description: When an active plan has gone untouched for a while, say so in one line.
x_kind: reminder
x_active: true
x_trigger: after `steps` recorded steps without plan_update ...
x_variables: path, steps
x_param_steps: 25
---
The plan ({{path}}) has not been updated in {{steps}} steps. ...
```

- `id`, `title`, `description`: identity and what the Reminders tab shows.
- `x_kind: reminder`: the file is an injection; its body is a sandboxed Jinja
  template (below).
- `x_active`: the person's switch (`false` silences it; traced `injection_inactive`).
- `x_required`: why the reminder cannot be switched off (it carries data the agent
  needs). Read from core's shipped file only: a copy cannot clear it, and a plugin's
  `x_required` is ignored (`clio-agent plugin check` warns `injection_required_ignored`).
- `x_trigger`: when it fires, in words, shown on the Reminders tab.
- `x_variables`: the names the code supplies; the body may use only these and the
  session state (`plan`, `todos`, `session`, `context`).
- `x_param_<name>`: the reminder's own settings (a threshold such as `steps`), read
  by the code that fires it.
- `x_overlay: true`: a settings overlay (below); it has no body.

The registry strips the `x_` prefix in its API rows (`metadata.kind`, `metadata.active`...).

## Validation

A reminder's body must parse as a template of the sandboxed Jinja environment
(`clio_agent.injections.template`: variables, ternaries, `{% if %}`; nothing runs)
and use only its declared variables and the session state. A copy that fails is
reported with its `validation_errors` and is not used: the shipped text applies
instead. A shipped template that fails is a bug: rendering raises the typed
`injection_template_invalid` error. Other prompt files keep the old
`{{name}}` placeholder check against the render context.

## Scopes and a person's copies

Copies are read in increasing precedence: built-in, then global
(`<config>/prompts`), workspace and session. A person's copy is one of two kinds:

- a **text edit**: the whole prompt with its own body (the Prompts editor);
- a **settings overlay** (`x_overlay: true`, no body): only the switch and the
  settings that differ from the shipped ones. The text stays the shipped one, so a
  later release's wording still reaches the person.

The Reminders tab's switch writes an overlay: `PUT /v1/prompts/{id}` with only
`metadata` (`active`, `param_*`) is accepted for a reminder, and a request that
sends the unchanged shipped text back is treated as no text override. When nothing
differs from the shipped settings, the person's copy at that scope is removed.
`clio_agent.injections.update` (used by the Fleet page's explore switch) writes the
same overlay, and keeps a global text edit if the person made one.

## API

The `/v1/prompts` routes (`gact/routes/prompts.py`): list (core's and the installed
plugins' reminders, with their sources), get one, render with sample values,
validate inline text, reload from disk, and save to a scope (global, workspace or
session).
