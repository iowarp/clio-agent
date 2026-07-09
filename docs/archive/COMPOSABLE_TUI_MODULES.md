# Composable TUI Module Layout

## Purpose

Design a TUI layout system where left and right sidebars are optional,
configurable, and composed from reusable modules. Sessions, context, experts,
memory, permissions, tools, prompts, and future features should be modules
rather than hardcoded permanent panels.

This is primarily a gact-tui product architecture issue, with CLIO providing
the backend data surfaces each module consumes.

## Motivation

The TUI is accumulating more useful surfaces:

- sessions;
- context and attached files;
- hierarchical experts;
- memory/context truth;
- permissions and policies;
- tools and MCP;
- prompt profiles;
- provider/model status;
- future NDP and science workflow modules.

A fixed sidebar model will not scale. Users should be able to choose which
modules appear, where they appear, and how compact they are.

## Desired Model

### Module

A module is a reusable UI provider with:

- stable id;
- title/icon;
- default placement;
- data loader;
- renderer;
- action handlers;
- search/filter support when applicable;
- compact/expanded modes;
- keyboard and mouse actions;
- capability requirements;
- persistence key for user layout state.

### Layout

The layout should support:

- optional left bar;
- optional right bar;
- hidden/collapsed bars;
- user-configured module order;
- module-specific width hints;
- compact and expanded module states;
- responsive behavior for narrow terminals.

### Configuration

Configuration should be editable both:

- from filesystem documents; and
- from inside the TUI.

The on-disk format should be human-editable and robust to unknown future module
ids. The TUI editor should validate before saving and offer rollback/defaults.

### Module Factory

The implementation should avoid hardcoding every sidebar branch in one place.
A module factory/registry should map module ids to module constructors and
capability requirements. Unknown or unavailable modules should be disabled with
a visible reason rather than crashing the layout.

## Candidate Modules

- `sessions`: session list and session actions.
- `context`: attached files, context pressure, selected file mentions.
- `experts`: hierarchy, selected expert, routing path, fallback warnings.
- `memory`: retained context, compact summaries, context-frame inspection.
- `permissions`: pending decisions, policies, audit history.
- `tools`: tool catalog, MCP servers, schemas, recent tool calls.
- `prompts`: active prompt profile, prompt provenance, edit affordance.
- `providers`: active model/provider state and warnings.
- `tasks`: session tasks/schedules.

## CLIO Backend Considerations

Most modules should consume existing GACT endpoints, but some planned modules
will need stronger backend support:

- expert hierarchy and delegation provenance;
- prompt catalog/profile endpoints;
- memory/context-frame endpoints;
- richer permission policy/audit views;
- file mention/context attachment provenance.

The module system should treat missing backend capabilities as normal and show
disabled states.

## gact-tui Work

- Define module interfaces.
- Extract existing session/sidebar behavior into a `sessions` module.
- Add optional right bar plumbing.
- Add layout config loading/saving.
- Add a TUI layout editor.
- Add search/filter/compact conventions shared by modules.
- Add tests for layout persistence, unknown modules, narrow terminal behavior,
  mouse selection, keyboard focus, and capability-gated modules.

## Acceptance Criteria

- Users can enable, disable, reorder, and move sidebar modules through config.
- Users can edit layout from inside the TUI and persist it.
- The TUI supports an optional right sidebar without breaking current left-bar
  workflows.
- Existing sessions behavior works as a module.
- Modules can declare required backend capabilities and render disabled states.
- Narrow terminals remain usable.
- Unknown future modules in config do not crash startup.

## Related Issues

- Primary implementation issue: create in `iowarp/gact-tui`.
- CLIO companion issues may be needed per module when backend surfaces are
  missing, but the layout/module factory belongs in the TUI.
- Hierarchical experts, prompt system, memory refinement, permission surfacing,
  and file mentions should all eventually provide modules.
