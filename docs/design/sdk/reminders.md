# Reminders: every injection as a Markdown template

Status: design, 2026-11-02. Tracks the owner's direction: every piece of text CLIO adds to
what an agent sees is a Markdown template that a person can switch off, edit and preview in
the UI; a trigger system comes later.

## What a reminder is

An *injection* is text CLIO adds to the conversation the model reads, recorded in the
transcript as an `injection` part (`gact/injection_parts.py`). About twenty sources exist
today; most build their text in Python:

| Source | Owner module | Fires |
| --- | --- | --- |
| `session_facts` | `gact/working_state.py` | when the session's facts change, and after a compaction |
| `plan`, `todos`, `plan_mode` | `gact/session_plan.py`, `gact/todos.py`, `gact/plan_mode.py` | turn start when changed; after a compaction |
| `plan_stale` | `gact/working_state.py` | after `plan.stale_steps` steps without a plan or todo write |
| `explore_nudge` | `gact/explore_nudge.py` | after N read-only calls without delegating, once per turn |
| `task_result` | task notifications | when a child or background task finishes |
| `plugin_change` | `gact/agents/prompt_basis.py` | when the agent's plugins or a deferred server's tools change |
| `summarization`, `compaction_failed`, `compaction_skipped` | `gact/summarization_record.py` | around a compaction |
| `memory` | `gact/memory/snapshot.py` | turn start, when memory applies |
| `answer_hook`, `hook_defer` | `gact/hooks/` | when a hook returns text or defers |
| `variant_advice`, `variant_drafting` | `gact/agents/variant_*.py` | best-of-N and refine |
| `mcp_elicitation` | `gact/elicitation_schema.py` | an MCP server asks a question |
| `result_spilled` | tool result bounding | a result over the inline limit |

Claude Code keeps the same kind of text as files (`system-reminder-*.md` in the
Piebald-AI mirror), each with declared variables and conditionals filled in by the harness.

## The template

Each reminder is a prompt in the existing registry (`src/clio_agent/prompts.py`), so it
inherits built-in, user and workspace scopes, editing (`save`) and rendering:

```markdown
---
id: clio.reminder.explore_nudge
kind: reminder
title: Suggest Explore while searching
description: After a run of reading calls, remind the agent that explore exists.
active: true
variables: [calls, explore_model, spawn_tool]
params: {after: 6}
trigger: after `params.after` read-only calls in a turn with no delegation; once per turn
---
You have made {{calls}} reading calls in this turn without delegating. ...
```

- `active` is the person's switch (distinct from the registry's validity check).
- `variables` declares what the code fills in; a placeholder outside the list is a
  validation error, shown in the editor. Values are text; nothing in a template runs.
- `params` are the reminder's own settings (a threshold); the code reads them from the
  resolved template, so the reminder page is the one place to change them.
- `trigger` describes when it fires. It is documentation until the trigger system exists.

Code calls `render_reminder(source, variables)` (new owner module `gact/reminders.py`),
which resolves `clio.reminder.<source>`, returns `None` with a typed
`reminder_inactive` trace when it is switched off, and otherwise renders the declared
variables. Scopes resolve as for every prompt: a workspace override beats a user one,
which beats the built-in.

## The UI

Settings, Reminders (gact-tui): every `kind: reminder` prompt with its title, description
and trigger; a switch for `active`; a Markdown editor listing the declared variables and
params; a preview rendered with sample values (`/v1/prompts/{id}/render`); reset to the
built-in. The Fleet page's Explore suggestion becomes a link to its reminder rather than a
second switch.

## Order

1. `gact/reminders.py`, the `kind`, `active`, `variables`, `params` frontmatter fields, and
   the explore and stale-plan notes moved to templates (the conf keys
   `delegation.explore_nudge*` and `plan.stale_steps` are retired in favour of `active` and
   `params`, pre-release).
2. The Reminders page.
3. The remaining sources, in batches, each with its rendered text pinned by its tests.
4. Later: declarative triggers in the frontmatter, evaluated by the loop, so a person can
   add a reminder without code. `$(command)` values are an open owner decision (user
   configuration only, sandboxed, run once per session).
