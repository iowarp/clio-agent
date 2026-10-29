# Plugins, agents and flows

Reference for how CLIO agents are built and run: Agent Plugins packages, the
Python Agent SDK (`clio_agent.sdk`), resolution and session pinning, user
agents, trust, the Claude Code importer, `clio-agent plugin check`, the REST
routes, and flows. It describes the code under `src/clio_agent/sdk/`,
`src/clio_agent/plugins/` and `src/clio_agent/flows/`. The design rationale is
in [design/sdk/](design/sdk/README.md).

An agent is a flow, an identity and a set of plugins. Plugins hold all content:
tools, MCP servers, skills, hooks, defined subagents, flows, commands, prompt
fragments and A2UI catalogs. Agents hold only their flow, their identity
(`role`), their plugin selection and their model requirements.

## Plugin layout

A plugin is an [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
package. CLIO content lives under the `ai.iowarp.clio` namespace.

```text
pdf/
├── plugin.json          # Agent Plugins 1.0.0 manifest
├── skills/              # portable: <name>/SKILL.md
├── mcp.json             # portable: MCP servers
└── ai.iowarp.clio/
    ├── plugin.py        # identity, requirements, metadata only
    ├── tools/*.py       # Python tools and claimed built-in tools
    ├── hooks/*.py       # @hook functions
    ├── subagents/*.py   # defined subagents
    ├── flows/*.py       # Flow classes
    ├── commands/*.md    # slash commands
    ├── prompt/**/*.md   # prompt fragments and prompt files
    └── a2ui/<id>/       # A2UI catalogs (catalog.json)
```

Every folder is optional. Python under `ai.iowarp.clio/` is imported only when
the plugin is trusted (see [Trust](#trust)); the folder is imported as a private
package, so plugin modules may import each other relatively.

### plugin.json

`plugin.json` may hold only the ten Agent Plugins fields: `$schema`, `name`,
`version`, `description`, `author`, `homepage`, `repository`, `license`,
`keywords` and `extensions`. An unknown field is a warning and is ignored.
`$schema` must be `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`.
`name` is the plugin id: 1 to 64 characters of `a-z`, `0-9`, `-` and `.`,
starting and ending alphanumeric.

The `extensions["ai.iowarp.clio"]` object accepts:

| Field | Meaning |
| --- | --- |
| `kind` | `plugin` (default) or `pack` |
| `requires` | Hard requirements: plugin references (`"shell"`, `"pdf@^1"`) |
| `members` | Pack members (packs only, required for a pack) |
| `model` | Model requirements (`tool_calling`, `vision`, `context_tokens`, `output_tokens`) |
| `prefers` | Model preferences (`model`, `provider`, `effort`) |
| `workflow_state` | The typed `workflow_state` vocabulary (validated against `WorkflowStateSchema`) |
| `catalogs` | Ids of built-in A2UI catalogs the plugin brings (`core` lists `clio-workspace`) |
| `connections` | Saved MCP connections the plugin uses: tool namespace to connection name (`{"home": "home-assistant"}`) |

Any other field in the namespace fails the load.

### plugin.py

`plugin.py` declares exactly one `Plugin` or `Pack`, as a subclass or an
instance. It carries identity and requirements only. When both `plugin.py`
and `plugin.json` state the id, version, kind, `requires` or `members`, they
must agree (`plugin_declaration_conflict`).

```python
from clio_agent.sdk import Pack, Plugin, Requires

class Manufacturing(Plugin):
    id = "manufacturing"
    version = "1.2.0"
    requires = ["shell"]
    model = Requires(context_tokens=64_000)

documents = Pack("documents", ["pdf", "docx", "xlsx", "pptx"])
```

### Tools

`ai.iowarp.clio/tools/*.py` contributes two kinds of tool:

```python
from clio_agent.sdk import native_tool, tool

shell_bash = native_tool("shell_bash")        # claim a CLIO built-in tool by name

@tool(read_only=True)
def plato_optimize(mesh: str, target: float) -> dict:
    """Optimize a mesh."""
```

A built-in tool reaches an agent only through the plugin that claims it; the
plugin that owns a capability brings its tools. Claiming a name CLIO does not
have is `native_tool_unknown`. Tool names are global: two resolved plugins
exposing the same name is the resolution error `tool_collision`.

### MCP servers

`mcp.json` holds exactly `$schema`
(`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`) and `mcpServers`.
Each entry is either:

- `stdio`: `command` (a bare executable name or a `./` path inside the plugin),
  `args`, `env`, `cwd` (`./...`, `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`);
- `streamable-http` or `sse`: `url` (https, or http on loopback) and `headers`.

An invalid entry is skipped and reported as `mcp_server_invalid`. Servers start
lazily on first use, in the session workspace rather than the plugin root.
`${PLUGIN_ROOT}` (the installed plugin folder) and `${PLUGIN_DATA}` (a persistent
per-plugin folder under the plugin store) are expanded in `args`, `env` and `cwd`
and set in the server's environment. A declaration that references
`${CLIO_PROVENANCE_CONFIG}` also receives the path of a YAML file with the
effective provenance configuration. Two resolved plugins declaring the same
server name is `mcp_server_collision`. A server's tools are namespaced by the
server name (`web_...`).

### Skills

`skills/<name>/SKILL.md` follows the Agent Skills format. The frontmatter `name`
must equal the folder name (lowercase letters, digits, single hyphens, up to 64
characters) and `description` must be 1 to 1024 characters; a skill that breaks
these rules is skipped with a `skill_invalid` warning. Resolved skills are
listed to the model by name and description and loaded on demand
(`load_skill`). Two resolved plugins providing the same skill id is
`skill_collision`.

### Hooks

```python
from clio_agent.sdk import HookEvent, hook

@hook("tool_result", after="core.audit", tools=r"shell_bash")
def redact_paths(ev: HookEvent) -> None: ...
```

`@hook(event, *, id="", before=None, after=None, tools="", fail_closed=False)`.
Events map one to one onto CLIO hook events:

| SDK event | CLIO event | SDK event | CLIO event |
| --- | --- | --- | --- |
| `tool_call` | `PreToolUse` | `session_end` | `SessionEnd` |
| `tool_result` | `PostToolUse` | `subagent_start` | `SubagentStart` |
| `tool_batch` | `PostToolBatch` | `subagent_stop` | `SubagentStop` |
| `prompt_submit` | `UserPromptSubmit` | `pre_compact` | `PreCompact` |
| `stop` | `Stop` | `before_model` | `BeforeModel` |
| `session_start` | `SessionStart` | `after_model` | `AfterModel` |
| `answer` | `Answer` | | |

An `answer` hook sees the settled answer in `ev.answer` and may return `note`,
`check` (`{"passed": bool, "text": str}`) and `answer` (a revision shown with
`reason`; the original is kept). A hook times out after 30 seconds.

A hook's id is `<plugin>.<id or function name>`; `before`/`after` names without
a dot are qualified with the same plugin. `tools` is an anchored regular
expression over tool names. A hook returns `None` to allow, a mapping with
`decision` (`allow`/`deny`), `reason` and `additional_context`, or raises
`PermissionError` to deny. `fail_closed=True` makes an exception inside a
deny-capable hook deny the call.

Resolution orders a session's hooks child to parent (plugins added by a
subclass or by the user's delta run before those the parent carried), then
applies `before`/`after`; a cycle is the error `hook_cycle`. Plugin hooks run
ahead of the user, project and managed `hooks.json` entries described in
[HOOKS.md](HOOKS.md), and are trusted with their plugin.

### Defined subagents

```python
from clio_agent.sdk import PromptFile, Subagent

reviewer = Subagent(
    "process-reviewer",
    prompt=PromptFile("prompt/reviewer.md"),   # relative to ai.iowarp.clio/
    description="Reviews a process run.",
    plugins=["pandas"],
    tools=["fs_read_file", "pandas_describe"],
)
```

Fields: `id`, `prompt` (a `PromptFile` or literal text), `plugins`,
`description` (tells the delegating agent when to use it), `tools` (optional
allowlist), `prefers`, `flow` (default `react`), `max_iters` and
`structured_outputs` (for example `{"workflow_state": True}`). A missing prompt
file is `prompt_file_missing`.

A defined subagent arrives and leaves with its plugin. Its child session
resolves `core`, the owning plugin and its own `plugins`, and composes its
prompt the same way as the main prompt, with the subagent's prompt in the
identity section. A `tools` allowlist must name tools those plugins provide
(`subagent_tool_unavailable`) and narrows only what they add: `core`'s tools
always stay. A subagent session's plugins come from its definition; they cannot
be changed in the live session (`subagent_plugins_fixed`). General subagents,
started at runtime with only a task prompt, come from the `delegation` plugin
(`spawn_agent_task`, `spawn_agents_parallel`).

### Prompt fragments

```markdown
---
id: shell.prefer-gh
section: capabilities
when: {tools: [shell_bash]}
---
For GitHub work, use the GitHub CLI (`gh`) through the shell.
```

A Markdown file under `prompt/` with a `section` in its frontmatter is a
fragment; one without is a plain prompt file (used by `PromptFile`).

- `id` must start with `<plugin>.`.
- `section` is `guidelines`, `rendering`, `capabilities` or `domain`. Only
  `core` may contribute `guidelines` and `rendering`.
- `when` may hold `tools` and `plugins` lists. A `tools` entry holds when a
  resolved tool has exactly that name or starts with `<entry>_`; every listed
  plugin must be resolved. A fragment whose `when` does not hold is left out.
- `generate` names a generator that appends generated text (`core.rendering`
  uses `ui_catalog`, the components of the session's A2UI catalogs).
- Templating is limited to `{{agent.id}}`, `{{agent.name}}`, `{{agent.role}}`
  and `{{env.NAME}}`.
- The budget is about 150 tokens (four characters per token). A larger
  non-generated fragment produces a `fragment_budget` warning, not an error.

The composed main prompt has five sections in a fixed order: identity
("You are {name}, {role}.", from the agent), guidelines, rendering,
capabilities and domain. Within a section, fragments follow plugin resolution
order, then file order. The resolution records each fragment with its plugin,
section, token estimate and path, which the UI uses to attribute the prompt.

### Commands and catalogs

`ai.iowarp.clio/commands/*.md` (or `commands/*.md` in an imported Claude Code
plugin) become slash commands in sessions whose agent resolves the plugin.
`ai.iowarp.clio/a2ui/<id>/catalog.json` declares an A2UI catalog; the resolved
set of catalogs decides what the session can render and feeds the generated
rendering section.

### Packs

A pack is a plugin whose content is only membership: `kind: "pack"` and a
non-empty `members` list. A pack with skills, servers, tools, hooks, subagents
or flows is `pack_invalid`. Pack membership is removable: removing one member
leaves the rest. A pack's own `requires` are hard requirements like any
plugin's.

## Agents

Agents are Python classes in a marketplace's `agents/*.py`:

```python
from clio_agent.sdk import Agent, Flow, Requires

class StandardAgent(Agent):
    id = "standard"
    version = "1.0.0"
    name = "CLIO"
    role = "an autonomous scientific coding and data assistant"
    plugins = Agent.plugins + ["workspace", "shell", "documents", "delegation"]
    requires = Requires(tool_calling=True)

class Researcher(StandardAgent):
    id = "researcher"
    role = "a research agent that verifies its sources"
    plugins = StandardAgent.plugins + ["deep-research@^0.3"]
    flow = Flow("deep-research", max_rounds=3)
```

| Attribute | Meaning |
| --- | --- |
| `id`, `version`, `name`, `description` | Identity; `name` defaults to `id`, `description` to the docstring |
| `role` | Fills the identity section of the composed prompt |
| `plugins` | Plugin references; `Agent.plugins` is `["core", "memory"]` (`memory` may be dropped) |
| `flow` | A flow id, a `Flow` class, or `Flow("id", **options)`; default `react` |
| `requires` | `Requires(tool_calling, vision, context_tokens, output_tokens)` |
| `prefers` | `Prefers(model, provider, effort)`; never blocks a session |

A subclass's `plugins` list replaces its parent's, so dropping a parent plugin
is allowed, but every agent must carry `core`. Each class that declares
`plugins` is a layer; a plugin belongs to the layer that first added it, which
is what the provenance shown in the UI and the hook order use. Plugins are
referenced by id with an optional npm-style range (`^1.2`, `~1.2`, `>=1.2`,
`<2`, `=1.2.3`, comma-joined, `*`), never imported. `<marketplace>/<id>`
picks one of several installed marketplaces that provide the same id.

## Resolution

**Stage 1, install.** Installing a marketplace snapshots its files, loads every
plugin into a `PluginRecord`, imports each agent class once and stores its frozen
`AgentSpec` (`Agent.spec()`) with a checksum. Sessions read only this store.

**Stage 2, session start.** `resolve_agent` turns a spec, the installed records
and an optional user delta into a `Resolution`:

1. Expand packs to their members.
2. Apply the delta: additions, then removals. Removing a pack removes the
   members it brought; removing a member removes only it.
3. Pull in hard requirements. Removing a plugin that others require removes
   them too (`reason: cascade`, `because: <plugin>`). `core` cannot be removed
   (`core_not_removable`); removing a plugin the set never had is the warning
   `removal_not_present`.
4. Check each plugin: installed (`plugin_not_installed`), a version in range
   (`plugin_version_mismatch`), not ambiguous between marketplaces
   (`plugin_ambiguous`; the agent's own marketplace, the built-ins and the
   default marketplace are preferred in that order, and a `<marketplace>/<id>`
   reference names one), valid, and trusted if it has executable parts
   (`plugin_untrusted`).
5. Collect tools, MCP servers, skills, subagents, flows, catalogs, commands and
   at most one `workflow_state` vocabulary; any duplicate is a collision error.
   The selected flow must be provided by a resolved plugin (`flow_missing`).
6. Union the model requirements of the agent and every resolved plugin, with
   attribution naming which plugin needs what.
7. Order hooks and compose the main prompt.

Any error raises `ResolutionError` with every typed diagnostic. A successful
resolution carries each plugin's provenance (`core`, the agent layer, `pack
<id>`, `required by <id>`, or `user`), what was removed and why, and a content
digest that excludes install paths.

**Session pin.** A new session resolves its agent (the requested ref, else the
session defaults' `agent_ref`, else `clio/standard`) and pins the result under
`<store>/resolved/<digest>.json`. Session metadata holds `agent_ref`,
`agent_delta` and `agent_resolution` (the digest); a defined subagent's session
also holds `agent_subagent`. Parent updates and newly installed versions reach
the next session. Adding or removing a plugin in a live session reruns stage 2
and pins the new resolution for that session.

## User agents

A user agent is a parent plus a delta, never a flattened plugin list. It is
stored as `<store>/user-agents/<id>.json`:

```json
{"id": "my-default", "name": "My default", "extends": "clio/standard@^1",
 "add": ["clio-hdf5"], "remove": ["xlsx"], "description": ""}
```

Saving without a range pins `extends` to the parent's major version, so the
saved agent follows its parent's updates within that major. A delta that does
not resolve is refused before anything is stored. A user agent may extend
another user agent; the chain flattens into one delta over the shipped root
agent. User agents are addressed as `user/<id>`.

## Marketplaces and the plugin store

A native marketplace is a folder (or git repository) with:

```text
ai.iowarp.clio/marketplace.json   # {"name": ..., "description": ...}
plugins/<id>/plugin.json ...      # one Agent Plugins package per folder
agents/*.py                       # Agent classes
```

Agents are addressed as `<marketplace>/<id>`; a bare id works when it is
unambiguous (the default marketplace wins a tie), and `@<range>` constrains the
version. A single plugin folder can also be installed as its own marketplace.

The store lives at `<config>/plugins/` (override with `plugins.store_root` or
`CLIO_PLUGIN_STORE`): `state.json` (marketplaces, records, pending updates,
agent specs, trust), `snapshots/` (immutable copies records point into),
`sources/` (git fetch cache keyed by URL and commit), `data/<plugin>/`
(`PLUGIN_DATA`), `user-agents/` and `resolved/`.

`core`, `memory`, `fleet` and `orchestration` ship inside clio-agent
(`src/clio_agent/plugins/builtin/`, marketplace `clio-builtin`). The default
marketplace `clio` is installed or refreshed on first use from
`plugins.default_marketplace` / `CLIO_DEFAULT_MARKETPLACE`, else the
`external/clio-agent-marketplace` checkout, else
`https://github.com/iowarp/clio-agent-marketplace.git`. A failure is reported
as `default_marketplace_unavailable` (`GET /v1/plugins` returns it as
`default_marketplace_error`); other marketplaces stay usable.

### Built-in plugins

| Plugin | Contents |
| --- | --- |
| `core` | Carried by every agent, never removable; requires `fleet`. Fragments `core.guidelines`, `core.rendering` (generated from the UI catalogs) and `core.tasks` (the task lifecycle); task controls (`query_tasks`, `observe_tasks`, `wait_tasks`, `cancel_tasks`, `get_task_result`), agent controls, `ask_user`, planning, artifacts, goals, loops and schedules, workspace resources; the `react` and `workflow` flows; the `planning` skill; the `clio-workspace` catalog |
| `memory` | Carried by `Agent`, removable. Long-term memory tools (`memory_search`, `memory_read`, `create_memory`, `update_memory`, `forget_memory`, `propose_memory_change`), session recall (`session_search`, `session_read_summary`, `session_read_context_frame`), the `memory.use` fragment and the internal `curator` subagent |
| `fleet` | `list_models`, `usage` and `refresh_provider_models`; the `fleet.models` fragment and the `update-models` skill |
| `orchestration` | Requires `delegation`. The `run_flow`, `run_workflow` and `make_plugin` tools, the `council` and `arena` flows, the `orchestration.flows` fragment and the `author-a-flow` skill |

## Trust

| Source or change | Trust |
| --- | --- |
| Plugins shipped in clio-agent (`clio-builtin`) | Trusted |
| The shipped default marketplace (`clio`) | Trusted at install |
| Any other git or local marketplace | Needs the trust step |
| An update that changes executable parts | Held until the trust step runs again |
| An update that changes only text | Applies to the next session |

Executable parts are `mcp.json`, `.mcp.json`, `hooks/hooks.json`, every `.py`
file under `ai.iowarp.clio/`, and the scripts an imported hook command runs.
Each plugin record carries separate digests of its executable and text parts.

Before the trust step, an untrusted marketplace's plugins load only their text
parts (a `plugin_untrusted` warning), its agent classes are not imported
(`marketplace_untrusted`), and any agent that resolves one of its plugins with
executable parts fails with `plugin_untrusted`. The trust step
(`clio-agent plugin trust <name>` or `POST /v1/plugins/marketplaces/{name}/trust`)
records each plugin's current executable digest. A later update whose
executable digest differs is stored as pending (`pending_trust`) while the
previously trusted record stays active.

## Claude Code importer

Claude Code plugin layouts are imported into the same `PluginRecord`, with no
source given special treatment:

| Claude Code | CLIO |
| --- | --- |
| `.claude-plugin/marketplace.json` | A marketplace; plugin `source` is a relative folder or a git source pinned by commit (`github`, `git`, `git-subdir`, `url` with `sha`); an unpinned git source is `plugin_source_unpinned` |
| `.claude-plugin/federated-marketplaces.json` | Each entry pinned by commit installs as its own marketplace (with this one as `parent`) and needs its own trust step |
| `.claude-plugin/plugin.json` `dependencies` | Only dependencies and no content: a pack. Dependencies on a plugin with content: hard requirements |
| `.mcp.json` | MCP servers; `${CLAUDE_PLUGIN_ROOT}` becomes `${PLUGIN_ROOT}` |
| `skills/` | Skills, unchanged |
| `hooks/hooks.json` | Command hooks on `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`, `PreCompact`. Another event is the warning `hook_event_unmapped`; a matcher naming unmappable tools is `hook_matcher_unmapped`; a non-command hook is `hook_type_unsupported`. `codex.json`, `opencode.js` and similar files are ignored |
| `agents/*.md` | Defined subagents. `tools` map through the table below and add the providing plugin; an unmappable tool is the error `tool_unmapped` and the subagent is skipped. `model:` and `effort:` become preferences |
| `commands/*.md` | Commands |

Tool mapping: `Read` → `fs_read_file`, `Glob` → `fs_list_directory`, `Grep` →
`fs_search_text` (`workspace`; both are partial matches, so each use is the warning
`tool_mapped_lossy` naming what differs); `Write` → `fs_write_file`, `Edit` and `MultiEdit` →
`fs_edit_file` (`workspace`); `Bash` → `shell_bash` (`shell`);
`WebFetch` → `web_fetch`, `WebSearch` → `web_search` (`web`); `Task`, `Agent` →
`spawn_agent_task` (`delegation`). Index files outside the Claude Code format
(such as `.clio-kit/catalogue.json`) are not read.

## clio-agent plugin

```text
clio-agent plugin check PATH                  run the install resolver on a folder
clio-agent plugin check --installed NAME      ... on an installed marketplace
clio-agent plugin install SOURCE [--ref R] [--commit C] [--name N]
clio-agent plugin trust NAME                  the explicit trust step
clio-agent plugin update NAME
clio-agent plugin uninstall NAME
clio-agent plugin list                        marketplaces and their plugins
clio-agent plugin agents                      installed agents, user agents included
clio-agent plugin resolve [AGENT] [--add P] [--remove P] [--prompt]
```

Every command takes `--json`. `check` loads a plugin, marketplace or Claude Code
layout exactly as install does (trusting the folder being checked), resolves each
plugin beside `core` with a probe agent, and runs `spec()` plus full stage 2 for
every agent class. Missing prompt files, unresolved ids, tool collisions, hook
cycles and other errors fail it (exit code 1); fragment budgets warn. Git and
federated sources are checked after install (`--installed`). The same check is a
pytest fixture:

```python
pytest_plugins = ["clio_agent.plugins.pytest_plugin"]

def test_marketplace_resolves(plugin_check):
    report = plugin_check(Path(__file__).parents[1])
    assert report.ok, report.render()
```

The fixture uses an isolated store seeded only with the built-in plugins.

## REST routes

Failures return the standard error envelope with a typed `error` code; resolution
failures use `agent_resolution_failed` with every diagnostic in `details.errors`.

| Route | Purpose |
| --- | --- |
| `GET /v1/plugins` | Installed plugins and packs (the + menu catalog), with trust and pending updates |
| `GET /v1/plugins/marketplaces` | Installed marketplaces |
| `POST /v1/plugins/marketplaces` | Install `{source, ref?, commit?, name?}` |
| `POST /v1/plugins/marketplaces/{name}/trust` | The trust step |
| `POST /v1/plugins/marketplaces/{name}/update` | Re-fetch and reload |
| `DELETE /v1/plugins/marketplaces/{name}` | Uninstall (goes through the destructive-action guard) |
| `POST /v1/plugins/check` | `plugin check` on `{path}` or `{installed}` |
| `GET /v1/agents` | Installed agents and user agents |
| `GET /v1/agents/resolve?ref=&add=&remove=` | Resolve without pinning: provenance, removals, composed prompt |
| `POST /v1/agents/preview-removal` | `{ref, plugin, delta?}`: what else leaves, and which tools |
| `POST /v1/agents/user` | Save a user agent `{id, extends, add, remove, name?, description?}` |
| `DELETE /v1/agents/user/{agent_id}` | Delete a user agent (destructive-action guard) |
| `GET /v1/sessions/{sid}/agent` | The session's pinned resolution and live delta |
| `PATCH /v1/sessions/{sid}/agent` | `{ref?, add?, remove?}`: change the agent or replace the live delta, then pin |
| `GET /v1/sessions/{sid}/tasks/{handle}/flow` | The flow run checkpointed under a task (see [Flow run view](#flow-run-view)) |

## Flows

A flow is an agent's orchestration program: a `dspy.Module` whose
`forward(task, ctx)` calls subagents, agents, flows and tools through the
runtime context. A plugin ships flows in `ai.iowarp.clio/flows/`; an agent
selects one by id, and `react` from `core` is the default. `react` runs the
agent's ClioReAct loop through `ctx.react(task)`.

```python
from clio_agent.sdk import Flow

class Review(Flow):
    id = "review"
    max_concurrency = 4

    async def forward(self, task, ctx):
        draft = await ctx.call("writer", task)
        reviews = await ctx.gather(
            [ctx.call(r, draft.answer) for r in ("critic", "fact-checker")], limit=2
        )
        return await ctx.call(
            "writer", {"task": task, "draft": draft.answer, "reviews": [r.answer for r in reviews]}
        )
```

Every `ctx` call is an ordinary task, so a flow gets handles, Stop,
cancellation, mailbox delivery and restart recovery from the task layer. A flow
run has no task kind of its own: it is checkpoints under its owning assignment.

### ctx primitives

| Primitive | Does | Built on |
| --- | --- | --- |
| `ctx.call(target, input=None, *, evidence=None, key="", model="", label="")` | Runs a target and returns its `CallResult`; awaitable. `model` picks the child's model (else `Flow.model`, the child's preference, the run's model); `label` names the child in the UI | `Conversations.spawn`; a tool call for a tool |
| `ctx.start(target, input=None, ...)` | The same, returning the handle at acceptance (not for tools) | `Conversations.spawn` |
| `ctx.gather(calls, *, limit=None, return_when="all", return_exceptions=False)` | Runs calls concurrently, at most `limit` at once | `TaskOperations.wait` |
| `ctx.as_completed(calls, *, limit=None)` | Yields each result (or outcome) as it settles, with `index` set (`details["index"]` on an outcome) | `TaskOperations.wait` |
| `ctx.ask(question, schema=str, *, options=None, reason="", key="")` | Asks the human; `schema=bool` returns a bool, `options` the chosen option's value, which is the option text lowercased | A Question task |
| `ctx.step(fn, *args, **kwargs)` | Runs a side effect once and records its result | A checkpoint entry |
| `ctx.workspace` | The session workspace (`read`, `write`, `append`, `root`); run writes through `ctx.step` | The existing workspace |
| `ctx.depth`, `ctx.run_id` | How many flows this run is nested in; the run id | Flow metadata |
| `ctx.react(task)` | The agent's own ReAct program inside this assignment | `ClioReAct` |
| `ctx.publish(view)`, `ctx.record_event(event)` | The run's live view and events, for the flow run view | The checkpoint |
| `ctx.permission_receipts(*, refresh=False)` | A pinned snapshot of attributable tool-permission history for the run and its confirmed children | ARC semantic events |
| `ctx.tool_results(handle)` | Terminal tool results in a confirmed child subtree, read without starting work | Task history |

Call targets resolve in this order: a `Flow` object or class, `self`, a defined
subagent, a resolved flow id, one of the agent's tools, an installed agent
(`marketplace/id`). Anything else raises `FlowTargetUnknown`. `self` runs this
agent as a worker on its `react` flow with the caller's exact resolution, so a
flow's helpers never re-enter the flow; a flow recurses only by calling a flow.

`input` defaults to the flow's task. A child receives the input as its
assignment text plus only the evidence the flow passes explicitly
(`evidence=[handles]`, pinned as `input_task_refs`). A tool target takes a
mapping of arguments; a task-capable tool that returns an accepted handle is
waited on. A `CallResult` reads as a mapping (its outputs) or by attribute:
`answer` (empty for a tool), `value` (a tool's returned value), `response_text`
(the public response with addenda), `handle`, `model`, `index`,
`workflow_state`, then any output or `workflow_state` section by name.

The child's assignment text is `render_input(input)`: text as is, a single
`CallResult` as its `response_text`, anything else as JSON. `CallResult`s
nested in a list or mapping do not convert (the child sees their `repr`), so
pass `[r.value for r in ...]` or `[r.answer for r in ...]`. `gather` merges successful results'
`workflow_state` in call order and records a key two members set differently
in `ctx.conflicts` and as a `workflow_state_conflict` event.

### Durability and replay

Each primitive's invocation id is
`<run id>:<kind>:<call site>:<digest of target and input>:<occurrence>`, computed
in program order when the call is made. The call site is the file and line in
the flow's code; `key=` replaces it when calls start in an order a replay may
not repeat (the workflow flow keys each step's call by step id). The id is passed to
`spawn` as the invocation id, so the task handle is deterministic.

The run's journal (calls with their handles and statuses, step results, events,
the published view and the final result) is written as a `WorkflowCheckpoint`
under the owning assignment, which points at the latest checkpoint. On restart
`forward()` runs again from the top and each call looks itself up: a settled
call returns its result, a running one is re-attached and waited on, an
uncertain one raises `TaskStateUncertain`, and only a call that never reached
acceptance is submitted. A recorded run met with different flow code,
resolution or input raises `FlowReplayMismatch`. Flow code must therefore be
deterministic given its results: no clock, randomness or I/O outside `ctx`.
Model calls made directly in `forward()` may run again after a crash. When a flow
consumes a child's result, the child's result notice is withdrawn from the
mailbox so it does not also wake the session.

### Limits

Class attributes (or per-selection options) set limits that `ctx` enforces on
top of the runtime's inference capacity; `0` means no flow-level limit:

| Limit | Default | Enforced on |
| --- | --- | --- |
| `max_depth` | 0 | Calling a flow deeper than this |
| `max_concurrency` | 0 | Concurrent `ctx` calls in the run |
| `max_model_calls` | 0 | Agent, subagent, flow and `react` calls |
| `budget_tokens` | 0 | Input plus output tokens of admitted children's session trees, checked before each new call |

One run records at most 10,000 checkpoint entries; past that it ends with
`flow_checkpoint_limit`.

### Typed outcomes

A call returns its result or raises a typed outcome carrying the handle
(`clio_agent.flows.outcomes`). The runtime never resubmits an uncertain call,
and one gather member failing never cancels the others.

| Outcome | Reason |
| --- | --- |
| `CallFailed` | `call_failed`: the task failed or its result is unavailable |
| `CallAwaitingInput` | `call_awaiting_input`: the child yielded waiting for input (a `CallFailed`; `answer` is its last answer) |
| `CallStopped` | `call_stopped`: the child's turn stopped (a `CallFailed`) |
| `CallCancelled` | `call_cancelled` |
| `CallInterrupted` | `call_interrupted`: interrupted or never started |
| `TaskStateUncertain` | `task_state_uncertain` |
| `CallRefused` | `call_refused`: the task layer did not accept the call |
| `FlowTargetUnknown` | `flow_target_unknown` |
| `FlowNotTrusted` | `flow_not_trusted`: the flow's plugin is installed but not trusted |
| `FlowLimitExceeded` | `flow_limit_exceeded`, with the `limit` |
| `FlowCheckpointLimit` | `flow_checkpoint_limit` |
| `FlowReplayMismatch` | `flow_replay_mismatch` |
| `FlowStall` | `flow_stall`, with `stall`, `step`, `predicate` and `observed` |
| `GatherFailed` | `gather_failed`; `results` holds every member's result or outcome |

A run that ends on an outcome records it as the run's status and result.

### Starting flows from the model

The `orchestration` plugin's tools start a flow as owned work and return its
handle at acceptance. The flow runs in a child assignment of this agent, pinned
to the session's exact resolution; the model keeps working and the result
arrives through the mailbox. Stop ends only the model's wait; `cancel_tasks` on
the handle cancels the flow and its subtree.

- `run_flow(flow, input=None, options=None, model="")` starts a resolved flow,
  or a flow of an installed, trusted plugin the agent does not carry yet (which
  adds that plugin to the conversation). A flow of an untrusted plugin asks the
  person first. The flow's `task` is `input` as given (JSON stays JSON).
- `run_workflow(workflow, task="", editors=None, max_added_steps=None,
  max_edit_rounds=None, allowed_targets=None, approve_edits=False, model="")`
  validates the workflow (`workflow_invalid`) and starts the `workflow` flow.
- `make_plugin` writes a flow plugin under `<store>/authored/<name>`, installed
  untrusted; its first run asks the person.

### The workflow flow

A workflow is steps as data: each has an `id`, a `target` (a subagent, an
installed agent, a flow, a tool or `self`), an optional `input`, the steps it
waits on (`after`), a `description`, `options` (for a flow target only) and a
`model`.

```python
from clio_agent.sdk import Step, Workflow

workflow = Workflow(steps=[
    Step("survey", "evidence_researcher", input="$task"),
    Step("relate", "data_analysis_expert", after=["survey"]),
    Step("review", "independent_reviewer", after=["relate"]),
])
await ctx.call("workflow", {"workflow": workflow.to_dict(), "task": task,
                            "editors": ["independent_reviewer"], "approve_edits": True})
```

Step ids are lowercase letters, digits, `-` and `_` (up to 64); `task` is
reserved; dependencies must exist and form no cycle (`WorkflowError`). In an
input, `"$task"` is the workflow's task and `"$<step>"` a finished step's
`answer`: a whole-string reference keeps the value, a reference inside text
becomes its text. A step with no input receives `{"task", "instructions"
(its description), "inputs" (its dependencies' answers)}`, or just the task
when it has neither a description nor dependencies. A tool step's `answer` is
empty unless the tool returns a mapping with `answer`, so `$<tool step>` and
`inputs` pass empty text for other tool results.

Every step whose dependencies completed starts at once, with no barrier. A
step that waits on a failed or cancelled step is skipped. A step whose target
is a named editor runs with `workflow_add_step(step_id, target, input, after,
reason, options)` and `workflow_cancel_step(step_id, reason)`; these only
propose, and the workflow reads the proposals from the editor's settled result,
so a replay applies exactly the same edits. An added step waits on the
editor's step. Edits are bounded by `max_added_steps`, `max_edit_rounds` and
`allowed_targets` (the tighter of the caller's bound and the
`workflows.max_added_steps` / `workflows.max_edit_rounds` settings applies); a
rejected proposal is recorded with its reason. With `approve_edits`, each
proposed step waits on a yes/no Question. Only named editors may edit; the
human cancels steps through the task controls.

A failed step is first offered to each editor as a typed stall; a repair step
an editor adds replaces the failed step for its dependents. If none repairs it,
the workflow ends with `FlowStall("step_failed")`. A cancelled step ends the run
cancelled. A completed workflow returns the answers of its final steps (then
the earlier ones), `results` (each step's answer), `workflow` (steps with
status and handle) and the merged `workflow_state`.

### council and arena

- `council`: each member answers the same question independently from its own
  perspective, then one synthesis. Options: `members` (default three `self`
  workers), `perspectives` (cycled across members), `models` (one per member),
  `synthesizer` (a target; empty synthesizes inside the flow as a recorded
  step) and `synthesizer_model`. Returns the answer, the member handles and
  each member's model.
- `arena`: contestants attempt the same task (at most three at once) and a judge
  inside the flow picks the winner. Options: `contestants` (default three `self`
  workers) and `models` (one per contestant). The verdict is a recorded step and the votes are appended to
  `arena/votes.jsonl` in the workspace (a `votes_not_recorded` event without a
  workspace). A winner index out of range is `FlowStall("judge_invalid_winner")`.

### Flow run view

`GET /v1/sessions/{sid}/tasks/{handle}/flow` returns the run checkpointed under
the task: `run_id`, `flow`, `status`, `depth`, `root_handle`, `entries`, the
published `view` (for a workflow, its steps with each step's handle and task status,
proposals awaiting approval, editors and bounds), every recorded call, open
questions, events, and either `result` or the typed `outcome`. The UI draws the
live workflow from it.
