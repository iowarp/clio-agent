# CLIO Agent Plugins and Composition

Oct 6, 2026 · @Jaime

## Purpose

Every capability in CLIO, and every line of its main prompt, should have exactly one home: the plugin it belongs to. Agents never own content. They select plugins, and the system composes tools, hooks, catalogs and prompt text from that selection.

Today the same pdf skill is maintained in three agents and the a2a skill in four, so a single fix has to be copied everywhere. The standard agent's main prompt has the same problem: its general guidelines are copied into every other agent and drift apart.

This document covers the plugin model only: the plugin standard, how agents are composed from plugins, how the main prompt is composed, and how plugins evolve per user. The Python SDK that implements this model and the design for remote agents each have their own document.

## Starting point and what changed

The Sept 17 planning session replaced the Markdown blueprint system with Python-defined agents on the Agent Plugins 1.0.0 standard. Most of that holds; the composition model is new, and one assumption about external plugin sources was dropped.

| Sept 17 decision | Status now |
| --- | --- |
| Agents are authored in Python; users kept getting the AGENT.md format wrong with no feedback | Holds |
| Python is the authoring surface; a frozen `spec()` stored at install is the runtime contract | Holds |
| Trust split: pip-installed plugins are trusted, git-installed ones need an explicit trust step | Holds |
| Agent Plugins 1.0.0 portable core plus one `ai.iowarp.clio` namespace | Holds, with the gap that the core has no composition (see Plugin standard) |
| "Blueprint" disappears; install a plugin, pick one of its agents | Replaced: an agent is a composition of plugins, not something a plugin ships whole |
| Delete the AGENT.md loader and legacy `clio-pack.yaml` path in one change, no dual loader | Holds |
| Agents declare capability requirements and preferences; operators resolve providers | Holds |
| Plugin-qualified tool names avoid collisions | Replaced: a collision is a resolution error |
| Rewriting other repositories' plugin layouts to match ours | Dropped: any repository is just a source of servers and skills, and none gets special treatment |

New since Sept 17: agents as plugin sets with packs and deltas, complete-by-default agents, the composed main prompt, coexistence rules for competing plugins, and per-user self-optimizing plugins.

## Plugin standard

The plugin is the unit of everything: distribution, trust, composition and evolution. CLIO uses [Agent Plugins 1.0.0](https://agent-plugins.org/specification) as the package format and defines composition itself, because the standard does not.

**What the core gives us.** `plugin.json` permits only ten top-level fields: `$schema`, `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords` and `extensions`. The portable components are `skills/` in the Agent Skills format and `mcp.json` for MCP servers. Client-specific content goes under a reverse-domain namespace, both as a key in `extensions` and as a top-level directory, and the namespace owner defines its validation.

**What the core lacks.** There is no field for dependencies, bundles or requirements on other plugins, nothing about marketplaces, and no hooks, agents or commands in the core. All of that is ours to define inside `ai.iowarp.clio`.

**What a plugin can contribute.** A plugin is never only skills. It may contribute any of:

- MCP servers (portable core)
- Skills (portable core)
- Hooks
- A2UI catalogs and other catalog access
- Defined subagents and specific peers, meaning external agents the plugin needs
- Commands
- Custom Python tools
- Prompt fragments (see Composed main prompt)
- Providers such as structured-file providers, and flows (orchestration programs such as ReAct, a council or a campaign)
- Dependencies on other plugins, either pack membership or hard requirements

**Layout.**

```text
pdf/
├── plugin.json          # $schema pinned to 1.0.0; extensions: { "ai.iowarp.clio": { ... } }
├── skills/              # portable
├── mcp.json             # portable
└── ai.iowarp.clio/
    ├── plugin.py        # identity, requirements, metadata only
    ├── tools/           # Python tools
    ├── hooks/           # hooks
    ├── subagents/       # defined subagents
    ├── commands/        # commands
    ├── flows/           # orchestration flows
    ├── prompt/          # fragments
    └── a2ui/            # catalogs
```

**Sources.** Servers and skills can come from any repository or registry. No source is special; a plugin references what it uses, and the resolver fetches it.

## External plugins and trust

CLIO loads plugins published in other formats, such as the Claude Code plugin layout, by importing them at install into the same internal plugin record. There is one importer per format and a single loader in the runtime.

An external plugin brings what it has and nothing more: MCP servers and skills, and sometimes defined subagents or commands, which stay with that plugin. It has no prompt fragments or requirements, the same as any plugin that does not need them. Once imported, it is validated, resolved, placed in packs and agents, and removed from the + menu like a native plugin.

Learned copies work the same way. `hdf5_personal` declares `evolved_from` the external `hdf5`, `check_updates` watches that source, and `rebase` starts from its latest version. When a source publishes no versions, the import pins by commit hash so an upstream change is still well defined.

| Source or change | Trust |
| --- | --- |
| pip-installed package | Trusted by construction, as pip already is |
| Git repository or external marketplace | Explicit trust step at install |
| Update that changes executable parts (server commands, Python tools, hooks) | Trust step again before it applies |
| Update that changes only text (skills, fragments) | Applies to the next session without a new trust step |
| Personal evolved fork | Inherits its upstream's trust, since the optimizer never touches executable parts |

## Agents as plugin sets

An agent is a flow, an identity and a set of plugins. A flow is the agent's orchestration program; the default is ReAct, and the orchestration document covers the rest. Remove every plugin and what is left is a chatbot with no tools. The standard agent's pdf, docs, excel, a2a, shell and subagent capabilities are all plugins on the root agent, not subagents.

**Packs.** A pack is a plugin whose content is only membership in other plugins. `documents` is pdf, docx, xlsx and pptx. An agent can carry the whole pack or any two of its members; both resolve the same way.

**Shipped agents in Python.** A new class means a new identity. New capabilities arrive as packs, which keeps the class hierarchy shallow and makes "why does my agent have this?" answerable.

```python
manufacturing = Pack("manufacturing", [abaqus, tosca, plato, am_process])

class Factorio(ScienceAgent):
    id = "factorio"
    role = "a manufacturing and topology optimization agent"
    plugins = ScienceAgent.plugins + [manufacturing]
```

**User agents as deltas.** The + menu lets a user add or remove plugins, rename the result and save it to their own marketplace. It is stored as a parent plus a delta, never a flattened list, so a fix to pdf in standard reaches every descendant.

```yaml
id: my-personal-default
extends: clio/standard@^1
remove: [xlsx]
```

**Resolution.** Expand packs, apply additions, then apply removals. Removing xlsx works even though it arrived through `documents`. A saved agent follows its parent within the major version.

**Complete by default.** Every shipped agent works out of the box. Factorio ships with both Abaqus and Plato even though they compete. Removing plugins is an optimization of context, knowledge or startup cost, never a repair, so a user can never assemble a stack that does not work.

**Membership versus requirement.** Pack membership is removable: dropping plato from manufacturing is fine. A hard requirement takes its dependents with it: removing shell when the github plugin requires it is allowed and removes github as well, and the UI shows what else goes. Requirements are declared per plugin.

**Sessions are pinned.** A session freezes its resolved plugin set at start. Parent updates and newly evolved personal plugins apply to the next session. Adding or removing a plugin inside a live session produces a new resolved set and a new prompt for that session.

**Traceability.** The UI shows where each plugin came from: core, standard, science, a pack, or the user.

## Coexistence rules

Complete-by-default agents load competing plugins side by side, so the resolver has to keep them from colliding or contradicting each other.

- **Tool name collisions are errors.** If two plugins expose the same tool name, resolution fails with a message naming both plugins. Authors are pushed toward specific names such as `plato_optimize`, and the model sees readable names.
- **Hooks run from child to parent**, so pdf's hooks run before the science agent's. The SDK can adjust order with `before=` and `after=` referencing other hook ids. A cycle is a resolution error. Since every hook has an id, ordering can be exposed in the UI later.
- **Fragments describe, they never claim exclusivity.** The Abaqus fragment says what Abaqus does; it never says to use Abaqus for all topology optimization. Choosing between alternatives belongs to the pack's own fragment or to the user.
- **Everything-on stays cheap.** Tools load deferred, with only names and descriptions in context until a tool is used, and MCP servers start lazily on first use. That keeps complete-by-default affordable and makes removal a real optimization rather than a necessity.

## Composed main prompt

The main prompt is assembled from fragments contributed by the agent and its plugins, in a fixed sequence of sections. No agent carries a hand-written copy of shared guidance.

1. **Identity**: "You are {agent.name}, {agent.role}." Owned by the agent; a child agent replaces it.
2. **Operating guidelines**: universal behavior, owned by a `core` plugin that every agent carries.
3. **Rendering**: what CLIO can display (LaTeX, citations, typed parts, A2UI components), generated from the same catalog the UI renders from. This closes the formatting gap identified on Sept 17.
4. **Capabilities**: one fragment per plugin that needs one.
5. **Domain**: science, manufacturing and similar framing.

A fragment is a small file in its plugin's `prompt/` directory:

```markdown
---
id: shell.prefer-gh
section: capabilities
when: { tools: [shell] }
---
When working with GitHub, check whether `gh` is authenticated and prefer it.
```

**Rules.**

- **A guideline lives with the capability it describes.** "Prefer gh" belongs to the shell plugin, so every agent with a shell gets it and an agent without one never sees an instruction it cannot follow.
- **Fragments carry only what the model cannot infer** from tool schemas and skill descriptions. A paragraph restating what the pdf tools do does not belong. The validator enforces a soft size budget per fragment (a warning above 150 tokens to start), since the root prompt grows with every plugin.
- **Fragments stand alone.** None refers to another ("as described above"). Joint guidance for two plugins uses `when: { plugins: [a, b] }` and should be rare.
- **Fragments have ids**, so agent deltas drop or replace them the same way they add or remove plugins.
- **Templating is minimal**: agent identity and environment variables only, no logic, so a scientist can still read and edit a fragment.
- **Ordering is deterministic**: sections first, then plugin resolution order. Stable sections lead, so toggling a plugin only invalidates the prompt cache from the capabilities section down.
- **The UI shows the composed prompt** with each fragment attributed to its plugin.

**Subagents.** Capability plugins attach to the agent that carries them; they are not subagents. There are two kinds of subagent. A general subagent is launched by the agent at runtime with only a task prompt, such as another CLIO told to find a needle, possibly a standard agent on a different model; that ability comes from one delegation plugin. A defined subagent, such as a planner, is shipped by the plugin it belongs to and arrives or leaves with that plugin. A subagent's prompt is composed the same way from the plugins it is given, so a pdf fragment written once serves the root and any subagent carrying pdf.

**Migrating standard's prompt.** The current prompt is cut apart: the identity line stays with the agent, universal guidelines move to `core`, and each capability paragraph moves into the plugin it describes.

**Task guidance.** The task lifecycle snippet (`clio.runtime.tasks`), which teaches handles, waits and Stop versus cancellation to every task-capable root and child, becomes a `core` fragment, so it is written once and reaches every agent.

## Self-optimizing plugins

Plugins evolve per user from their own sessions, on a schedule, and running sessions never change underneath the user. Lineage lives on plugins, not on agent classes, so agents stay shallow while plugins carry their history.

**Lineage and the override slot.** An optimization run produces a personal fork such as `pdf_personal`, declared `evolved_from: pdf@1.5.0`. The user layer then applies one rule: wherever `pdf` appears in any agent, this user gets `pdf_personal`. When both marketplaces are available, the fork maps onto the original's slot, so every agent that includes pdf picks up the improvement without being edited.

**Timing.** The optimizer runs periodically (daily or weekly), offline or when the user triggers it. It publishes a new version of `pdf_personal`, and the next Factorio session resolves `pdf` to it. A running session keeps its pinned version.

**The cycle is one agent's job.** Nothing about it is mechanical. Each run regenerates the fork from the latest upstream plus the accumulated lessons rather than editing last period's fork, so an upstream release is absorbed by the rebase step instead of by a merge.

&#91;embedded content: Optimization cycle with two gates\]

A failure at gate 1 points to the upstream change; a failure at gate 2 points to the new lessons. Neither publishes.

**Lessons are discrete units.** Each lesson records what changed, which component it touches, and the evidence behind it, such as the session and failure it came from. The fork is an upstream version plus an ordered lesson list, so every published version is reproducible. This gives the agent clean units to reason about rather than a block of edited prose.

**Revert and promotion are agent tasks too.** A gate failure or a user request triggers a revert: the agent decides which lesson to drop, rebuilds without it and gates again. A lesson that many users' forks share can be proposed as a change to upstream `pdf`.

**What may evolve.** Text and parameters only: skills, prompt fragments and configuration. MCP server code and executable hooks are never rewritten by the optimizer, since that crosses the trust boundary set for plugin code.

**The gate.** The validator plus the plugin's evals, run on both upstream and the candidate. Learning from negatives can overfit, and without a gate a fork can quietly get worse. This depends on the benchmarks and evals work already scoped for 1.0.

## Decisions and open items

| Area | Decision |
| --- | --- |
| Package format | Agent Plugins 1.0.0 core plus the `ai.iowarp.clio` namespace |
| Composition | Defined by CLIO in the namespace, since the core has none |
| Agent | A flow, an identity and a set of plugins |
| Capabilities | Plugins on the agent that carries them, not subagents |
| Subagents | General subagents come from one delegation plugin; defined subagents ship with the plugin they belong to |
| Grouping | Packs; a new agent class only for a new identity |
| User agents | Parent plus delta, saved to the user's marketplace from the + menu |
| Defaults | Complete by default; removal is optimization |
| Dependencies | Pack membership is removable; hard requirements are declared per plugin, and removing one also removes its dependents |
| Tool names | A collision is a resolution error |
| Hooks | Child to parent, adjustable with `before`/`after`, cycles are errors |
| Cost | Deferred tool loading and lazy server startup |
| Main prompt | Composed from fragments in five fixed sections, attributed in the UI |
| Fragments | Live with their capability, stand alone, have ids, stay small |
| Sessions | Pinned at start; updates apply to the next session |
| Evolution | Periodic, agent-driven, regenerated from upstream plus lessons, gated after rebase and after new lessons |
| Evolvable parts | Skills, prompt fragments and configuration only |
| External agents | An external-agents pack (a2a, acp, academy) provides discovery and one fragment; specific peers are declared by the plugins that need them; agents own only their exposure |

**Deferred by design.**

- **Evals live in the plugin's repository** and are not parsed by CLIO. The optimization gate runs them; the plugin format does not need to describe them.
- **The lesson record schema and the personal marketplace come later**, in the optimization campaign. This design only leaves room for them: `evolved_from`, the override slot and session pinning.
