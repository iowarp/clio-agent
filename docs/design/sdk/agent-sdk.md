# CLIO Agent SDK

Oct 6, 2026 · @Jaime

Plugins hold all content, including the specific external agents they work with. Agents hold only their flow, their identity, their plugin selection and their exposure. Python is the authoring surface, and the frozen spec produced at publish is the runtime contract. The plugin model this SDK implements is described in the companion document on agent plugins and composition.

## Classes

**Plugin.** `plugin.py` carries identity, requirements and metadata only. Everything else is found by convention in the plugin's folders.

```text
manufacturing/
├── plugin.json          # Agent Plugins 1.0.0 manifest
├── skills/              # portable
├── mcp.json             # portable
└── ai.iowarp.clio/
    ├── plugin.py        # identity, requirements, metadata only
    ├── tools/           # Python tools
    ├── hooks/           # hooks
    ├── subagents/       # defined subagents
    ├── peers/           # specific external agents this plugin needs
    ├── flows/           # orchestration flows (ReAct, councils, campaigns)
    ├── commands/        # commands
    ├── prompt/          # fragments
    └── a2ui/            # catalogs
```

```python
# ai.iowarp.clio/plugin.py
class Manufacturing(Plugin):
    id = "manufacturing"; version = "1.2.0"
    requires = ["shell", "a2a"]                  # hard requirements, by id
    model = Requires(context_tokens=64_000)       # optional model requirement
```

```python
# ai.iowarp.clio/hooks/redact_paths.py
@hook("tool_result", after="core.audit")
def redact_paths(ev): ...

# ai.iowarp.clio/hooks/cite_check.py: sees the settled answer; never a silent swap
@hook("answer")
def cite_check(ev):
    cited = "http" in ev.answer
    return {"check": {"passed": cited, "text": "Cites a source." if cited else "No source."},
            "note": "Checked by the lab's citation policy."}
    # or {"answer": revised, "reason": why}: a visible revision, original kept

# ai.iowarp.clio/subagents/process_reviewer.py
reviewer = Subagent("process-reviewer",
                    prompt=PromptFile("prompt/reviewer.md"),
                    plugins=["manufacturing", "pandas"])

# ai.iowarp.clio/peers/tosca_runner.py
tosca_runner = Peer("tosca-runner", protocol="a2a",
                    skills=["topology optimization"])
```

**Pack.** A plugin whose content is only membership.

```python
documents = Pack("documents", ["pdf", "docx", "xlsx", "pptx"])
external_agents = Pack("external-agents", ["a2a", "acp", "academy"])
```

**Agent.** A new class means a new identity; new capabilities arrive as plugins and packs.

```python
class StandardAgent(Agent):
    id = "standard"; version = "1.0.0"
    flow = "react"
    role = "a general research assistant"
    plugins = ["core", "documents", "external-agents", "shell", "delegation"]
    requires = Requires(tool_calling=True, context_tokens=128_000)

class Factorio(ScienceAgent):
    id = "factorio"
    role = "a manufacturing and topology optimization agent"
    plugins = ScienceAgent.plugins + ["manufacturing@^1"]
    expose = Expose(a2a=True, acp=True)
```

**Subagent.** A defined subagent lives in the `subagents/` folder of the plugin it belongs to and has its own prompt and plugin set. General subagents, launched at runtime with only a task prompt, come from the delegation plugin and need no definition.

**Flow.** A flow is the agent's orchestration program, a `dspy.Module` whose `forward(task, ctx)` calls subagents, peers and tools through the runtime context. It lives in the `flows/` folder of the plugin that contributes it, and the agent selects one by id. `react` from `core` is the default. Every `ctx` call is an ordinary CLIO task, so flows get handles, Stop, cancellation and restart recovery from the task layer. The orchestration document defines the primitives and their semantics.

```python
# ai.iowarp.clio/flows/council.py
class Council(Flow):
    id = "council"
    members = ("physicist", "numerics-reviewer", "hpc-engineer")
    async def forward(self, question, ctx):
        views = await ctx.gather([ctx.call(m, question) for m in self.members])
        return await ctx.call("synthesizer", {"question": question, "views": views})
```

## Ownership

Every piece of behavior has exactly one home, so a change made once reaches every agent that uses it.

| Owner | Owns |
| --- | --- |
| Plugin | MCP servers, skills, Python tools, hooks, defined subagents, specific peers, flows, commands, prompt fragments, catalogs, requirements on other plugins, optional model requirements |
| Pack | Membership only |
| Agent | Flow, identity (`role`), plugin selection, model requirements and preferences, exposure |
| Operator or user | Peer bindings (address and credentials), deltas from the + menu, personal plugin overrides |

**Peers belong to plugins.** A specific external agent such as `tosca-runner` is something a capability needs, the same way it needs a server or a credential, so the plugin that needs it declares it. The ability to discover and call external agents at all comes from the `external-agents` pack, whose plugins (`a2a`, `acp`, `academy`) provide the client tools and one prompt fragment telling the agent it can discover the agents the user has connected.

**Exposure stays on the agent.** `Expose(a2a=True, acp=True)` describes the agent itself to the outside world rather than a capability it uses. The A2A card is generated from the resolved spec: the role becomes the description, and plugins and defined subagents become skills.

## Resolution

An agent is resolved twice: once at publish, the same for every user, and once at session start, for one user.

**Plugins are referenced by id with a version range, never by import.** `"manufacturing@^1"` works for external plugins with no Python, for user deltas saved as data, and for evolved forks filling a slot.

**Stage 1, publish.** `spec()` imports the agent once, flattens the class chain, and stores the frozen spec with its checksum. The spec names `pdf`, not anyone's fork, and is identical for every user.

**Stage 2, session start.** Applying Factorio to a user's CLIO is the step that makes it personal:

1. Expand packs to the full plugin set.
2. Apply the user's delta, if they saved one from the + menu.
3. Apply override slots, so `pdf` becomes `pdf_personal` where the user has an evolved fork.
4. Check requirements, peer bindings and the model requirement union.
5. Compose the main prompt and order hooks.
6. Pin the result for the session.

Parent updates and newly evolved forks reach the next session. Adding or removing a plugin inside a live session reruns stage 2 for that session.

## Requirements and validation

Every check that can fail at session start has a clear message and a clear fix, so an agent always works or says exactly why it does not.

- **Hard requirements.** Declared per plugin by id. Removing a required plugin from the + menu is allowed and also removes its dependents, and the UI shows which ones go. Pack membership is never a requirement.
- **Peer bindings.** A peer declared by a plugin is required configuration. The UI asks the user to fill its address and credentials, and the session does not start until the binding is filled or the plugin that needs it is removed.
- **Model requirements.** Plugins may declare them but do not have to. The effective requirement is the agent's own plus those of every resolved plugin, so removing pdf can relax the need for vision. When no available model satisfies the union, the error names which plugin needs what.
- **Collisions.** Two plugins exposing the same tool name, or a cycle in hook ordering, is a resolution error.
- **Development time.** `clio-agent plugin check` and a pytest fixture run `spec()` and the same resolver the install path runs, so unresolved ids, missing prompt files, collisions and fragment size budgets fail in the editor and in CI, not in a user's session.

## Where the Sept 17 fields went

The Sept 17 sketch let an agent own its prompt, experts, tools and skills. All of that moved into plugins.

| Sept 17 field | New home |
| --- | --- |
| `prompt` (root.md) | `role` fills the identity section; everything else is fragments in plugins |
| `experts` | Defined subagents in the plugin they belong to; general subagents through the delegation plugin |
| `tools`, `skills` | Plugins |
| Callbacks such as `on_tool_result` | Hooks in plugin `hooks/` folders |
| `a2ui` catalog | Plugin `a2ui/` folders |
| `expose` (A2A card) | Stays on the agent as exposure tick marks |
| `loop` (now flow), `requires`, `prefers` | Stay on the agent; plugins may add model requirements |
| Imports of other plugins' Python modules | Plugin ids with version ranges |
| `spec()` | Stays; now resolves packs, plugins, fragments and hooks |

## Settled in review

- **Agents live only in marketplaces** for now; there is no inline agent for scripts or notebooks.
- **Hooks run from child to parent**, so pdf's hooks run before the science agent's. `before=` and `after=` can still adjust a specific pair.
- **Fragments have a soft size budget.** The validator warns above 150 tokens per fragment to start; it is a warning, not an error.
- **A required plugin can be removed.** Removing it also removes the plugins that require it, and the UI shows which ones go.
- **Provenance is external**, like the provider and model: a choice made by the operator or the environment, not part of the agent definition.
