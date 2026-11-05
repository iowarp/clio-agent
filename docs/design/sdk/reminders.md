# Reminders: every injection as a Markdown template

Status: the injection system, 2026-11-03 (`src/clio_agent/injections/`). Every piece
of text CLIO adds to what an agent reads is a declared injection, from core or a plugin,
attached to an event; a person can switch it off, edit it and preview it in the UI.

## The injection system (2026-11-03)

- **Declarations.** One Markdown file per injection (flat frontmatter, body). Core's 72 ship in
  the core plugin (`src/clio_agent/plugins/builtin/core/ai.iowarp.clio/injections/`); a plugin
  declares its own in `ai.iowarp.clio/injections/*.md` (`name`, `event`, optional `tool`
  pattern, `title`, `description`, `active`). `prompt_packs/` is deleted.
- **Events.** The code decides only *when* an event fired and what it knows. General events,
  fired for every loop: `turn_start` (with the turn's other additions) and `tool_result`
  (after each call, appended to the result the model reads, narrowed by `tool:`; this is
  guidance in tool responses). Every core injection is also an event, named after it
  (`plan_stale`, `result_spilled`, `task_result`, `summarization`...), with its declared
  variables; a plugin may add text to any of them. Catalogue: `injections/events.py`.
- **Variables.** The event's own (`x_variables`), plus the session state any template may read:
  `plan` (`location`, `status`, `exists`), `todos` (`open`, `done`, `total`, `items`),
  `session` (`id`, `workspace`, `delegated`), `context` (`tokens`, `window`, `percent`),
  computed only when used.
- **Templates.** Sandboxed Jinja (`ImmutableSandboxedEnvironment`, `StrictUndefined`):
  ternaries and `{% if %}`, no code execution; a name or attribute nobody supplied is a
  typed `InjectionTemplateError`. A core or person's template that fails raises; a plugin's
  is left out with the typed reason `injection_plugin_render_failed` (trace and stream audit).
- **Edits.** A person's copy is saved through the prompt registry under `clio.reminder.<name>`
  (plugin injections: `clio.reminder.<plugin>.<name>`), per user, workspace or session; the
  registry validates an injection body with the same Jinja parser. A copy is a text edit
  (its own body) or a settings overlay (`x_overlay: true`, no body: only the switch and the
  settings that differ from the shipped ones), so switching a reminder never freezes its
  wording; the Reminders tab's switch and the Fleet page's explore switch write overlays.
  Installed plugins' injections are listed beside core's and switched the same way; only
  core's may be `x_required`.
- **Recording.** The recorder writes state (the plan, the todos, the session's facts) only
  when it differs from the copy still in context. An event note (`plan_stale`, `replan`,
  `explore_nudge`) carries a per-firing id and is recorded at every firing, even in the same
  words. The plan the agent wrote itself is held while its `plan_update` call is in context
  and offered again after a compaction, a rebuilt context or the person's edit.

## What a reminder is

An *injection* is text CLIO adds to the conversation the model reads, recorded in the
transcript as an `injection` part (`gact/injection_parts.py`). About twenty sources exist;
each source's code decides when it fires and fills the variables, and its wording is the
template (pieces built from conditions are one template each):

| Source | Owner module | Fires |
| --- | --- | --- |
| `session_facts` | `gact/working_state.py` | when the session's facts change, and after a compaction |
| `plan`, `todos`, `plan_mode` | `gact/session_plan.py`, `gact/todos.py`, `gact/plan_mode.py` | turn start when changed; after a compaction |
| `plan_stale` | `gact/working_state.py` | after the reminder's `steps` setting (`x_param_steps`, 25) of tool-calling steps without a plan or todo write, outside plan mode |
| `explore_nudge` | `gact/explore_nudge.py` | after `x_param_after` read-only calls without delegating, once per turn |
| `replan` | `gact/replanning.py` | the turn after the stall monitor flags little progress in an approved playbook |
| `child_report` | `gact/child_report_fit.py` | turn start of a session another agent started |
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

### As built

72 templates under `src/clio_agent/plugins/builtin/core/ai.iowarp.clio/injections/`, named
`<source>` or `<source>_<piece>` when the text is built from conditional pieces (one
template per piece; the code decides which apply, the template is the wording). The
defaults' rendered text is pinned by `tests/test_gact/test_reminders*.py`; 35 are
`x_required`, 37 can be switched off.

- **Switchable** (a person may turn them off): `explore_nudge`, `plan_stale`,
  `session_facts*`, `plan*`, `todos`, the optional `plan_mode_*` bullets,
  `compaction_failed`, `compaction_skipped`, the `context_recovery_*` notes, `memory*`,
  `plugin_change_deferred_tools`, `plugin_change_footer`, `circuit_breaker_warning` (a fact;
  nothing is blocked), `child_report`, `replan`,
  `path_hint`, `variant_drafting`.
- **Required** (`x_required`, reworded but never off; the reason is shown on the page):
  `plan_mode`, `plan_mode_playbook`, `summarization*`, `context_recovery_uncertain`,
  `result_spilled*`, `media_unavailable*`, `plugin_change` and its removed/added pieces,
  the eight `hook_*` notes, `hook_defer`, `variant_advice`, the
  `variant_drafting_*` pieces (switch the whole note instead), `mcp_elicitation`,
  `task_result*`. Only the shipped template can declare `x_required`.
- The recorder gates every source on `clio.reminder.<source>`; a source emitted directly
  is gated where its text is built, so the transcript and the model never disagree.
- Not migrated, on purpose: `answer_hook` (a hook's own text, shown to the person only)
  and the approval-card prompts (person-facing).
- Wording built once per session: the memory snapshot (an edit reaches new sessions).

## The template

Each reminder is a prompt in the existing registry (`src/clio_agent/prompts.py`), so it
inherits built-in, user and workspace scopes, editing (`save`) and rendering:

```markdown
---
id: clio.reminder.explore_nudge
title: Suggest explore while searching
description: After a run of reading calls, remind the agent that an explore child can take the rest of the search.
x_kind: reminder
x_active: true
x_trigger: after `after` read-only calls in one turn with no delegation, when the agent can start an explore child; at most once per turn
x_variables: calls, explore_model, spawn_tool
x_param_after: 6
---
You have made {{calls}} reading calls in this turn without delegating. ...
```

Frontmatter is flat (`key: value`); the registry strips the `x_` prefix in its rows.

- `x_kind: reminder` marks an injection: its body is validated as a sandboxed template.
- `x_active` is the person's switch (distinct from the registry's validity check).
- `x_required` says why it cannot be switched off; read from core's shipped file only.
- `x_variables` declares what the code fills in; a name outside the list (and the session
  state) is a validation error, shown in the editor. Nothing in a template runs.
- `x_param_<name>` are the reminder's own settings (a threshold); the code reads them from
  the resolved reminder, so the Reminders tab is the one place to change them.
- `x_trigger` describes, in words, when the code fires it. The events themselves exist
  (`injections/events.py`: `turn_start`, `tool_result` and one per core injection); a
  plugin attaches to one with `event:`.

Code calls `render_reminder(source, variables)` (now `clio_agent.injections.render`),
which resolves `clio.reminder.<source>`, returns `None` with a typed
`injection_inactive` trace when it is switched off, and otherwise renders the declared
variables. Scopes resolve as for every prompt: a workspace override beats a user one,
which beats the built-in.

## The UI

Settings, Reminders (gact-tui): every `kind: reminder` prompt with its title, description
and trigger; a switch for `active`; a Markdown editor listing the declared variables and
params; a preview rendered with sample values (`/v1/prompts/{id}/render`); reset to the
built-in. Plugin injections are listed beside core's. The Fleet page keeps its explore
switch and threshold; it writes the same reminder's settings overlay
(`explore_nudge.save_nudge_settings`), so the two never disagree.

## Order

1. `clio_agent.injections` (was `gact/reminders.py`), the `x_kind`, `x_active`, `x_variables`,
   `x_param_*` frontmatter fields, and the explore and stale-plan notes moved to templates
   (the conf keys `delegation.explore_nudge*` and `plan.stale_steps` are retired in favour of
   `x_active` and `x_param_*`, pre-release). Done.
2. The Reminders page. Done.
3. The remaining sources, in batches, each with its rendered text pinned by its tests. Done.
4. Later: declarative triggers in the frontmatter, evaluated by the loop, so a person can
   add a reminder without code. `$(command)` values are an open owner decision (user
   configuration only, sandboxed, run once per session).

## Later: event triggers (owner note, 2026-11-03)

Model: Claude Code's own harness. Attaching a repository returns a tool result that carries
that path's guidance (clone with a long timeout, what a 429 means, never delete a live
half-clone, pushes will be refused and why), and registering the clone makes the next turn
carry the repository's CLAUDE.md, skills and plugins as a reminder. Guidance arrives with
the event that makes it relevant, not in the base prompt. Three triggers to start with,
each a template whose frontmatter names its event:

- **On a tool result** (built: a declaration with `event: tool_result` and a `tool:`
  pattern, `injections/events.py`; core's own cut-result and path notes still fire from
  their code): the template is appended to that
  tool's result, so a tool's situational guidance (what to do next, what will be refused)
  costs nothing until the tool runs and is editable on the Reminders page. Today's ad hoc
  cases move here: the spilled-result note, path hints, the failure fact.
- **On a folder attach** (`x_trigger: folder_attached`): when a session gains a folder (a
  workspace attach, a mount, a child working elsewhere), that folder's `AGENTS.md` /
  `CLAUDE.md` arrives at the next step, as `session_facts` does. Today only the main
  workspace's files are read, at composition.
- **On a capability change** (`x_trigger: capability_changed`): a tool, server or plugin
  that becomes available or unavailable is stated as a fact at the next step
  (`plugin_change` today), with what the agent can do instead.

The rule stays: a triggered reminder states facts and conditions, never a ban on the model
in general.
