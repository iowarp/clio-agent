# Pack anatomy (as implemented, verified Sept 2026)

Check these references against current code before relying on them. Line numbers drift.

## Blueprint: `<pack>/AGENT.md`
**Where it's parsed:** `src/clio_agent/gact/agent_blueprints.py` (`parse_agent_blueprint_root`) and `validate_agent_blueprint_path`.

**Frontmatter:**
- **Required:** `id` (`^[A-Za-z0-9_.-]+$`).
- **Common:** `version`, `title`, `display_name`, `description`, `root_expert` (aliases `root`, `default_expert`, `default_root_expert`).
- **`defaults`:** e.g. `prompt_profile: heavy`.
- **`requires`:** e.g. `clio_agent: ">=0.9.4.17"`.
- **`mcp_servers`:** either a string (`hdf5: clio-kit mcp-server hdf5`) or a `command:` + `args:` mapping, plus `probe_timeout_retries`.
  - Only `${VAR}` expansion works, and a missing variable is an error.
  - **There's no pack-dir template.** The server's cwd is the session workspace, which is why `phenotype` anchors on `${LOCALAPPDATA}`. Agent Plugins' `${PLUGIN_ROOT}` will fix this.
- **`a2ui_catalogs`:** a builtin name, or `name: catalogs/<dir>`.
- **`workflow_state`:** validated against `gact/workflow_state/schema.py`. A bad schema disables the pack.
- **Also parsed:** `includes`, `compatibility`, `blueprint`, `install`.
  - **Check what `includes` does before assuming packs can compose.**
- **Not checked:** `blueprint: format: agent-blueprint-v1`.
- **`skills` isn't a manifest field.** Skills are declared per expert.

## Experts: `<pack>/experts/*.md`
**Where they're parsed:** `gact/expert_packs.py`.

**Fields:**
- **`id`** (required), **`tier`**; tier > 1 requires **`parent`**. `children:` is ignored; `parent:` defines the tree.
- **`tools`** (aliases `allowed_tools`, `allowed-tools`): the tool allowlist.
- **`skills`**, `commands`, `keywords`, `a2ui_catalogs`.
- **`module.kind`:** `predict`, `chain_of_thought` or `react`.
- Plus `structured_outputs`, `fanout`, `workflow`, `parameters.max_iters`, and model/provider overrides.
- A prompt body or `prompt_id`.

**Lesson from earthscope-flat:** deep expert hierarchies hurt smaller models. Prefer **one react expert + skills loaded on demand**, and add a child agent only where it buys isolation.

## Skills: `<pack>/skills/<name>/SKILL.md` (or a flat `<id>.md`)
**Where they're parsed:** `gact/skills.py`.

**Lookup order:** pack → catalog → workspace (`<cwd>/.claude|.codex|.agents/skills`) → global → builtin.

**Frontmatter:**
- `name` (the id; defaults to the directory name), `title`, `description`, `keywords`.
- `effect:`, one of `enter_mode`, `spawn_subagent_with_skill`, `loop`, `set_goal`, `schedule`, `plan_workflow`, `plan_small`.
- **Level tags go in `keywords`** (`level:L0|L1|L2`).

**Loading:**
- The system prompt lists only names and descriptions.
- `load_skill(skill_id, file="")` returns `Skill directory (use as SKILL_ROOT): …`, the body, and a list of bundled files.
- `file=` reads a bundled file, and can't escape the skill directory.

**Bundled scripts:**
- The agent runs them through `shell_bash`, using the SKILL_ROOT path.
- **No environment is provided.** Pattern to copy: `gact/builtin_skills/work-with-pdfs/SKILL.md`:
  `uv run --no-project --with <deps> python "SKILL_ROOT/scripts/x.py"`.
- Prefer PEP 723 inline dependencies in the script itself.
- Existing marketplace examples are stdlib only (`document-production/skills/*/scripts/`).

**Child agents:**
- A skill with `effect: spawn_subagent_with_skill` is started with `spawn_skill_task(skill_id, task)`, not `load_skill`. The child is seeded with the skill body plus the task.
- Collect the result with `wait_agent_tasks`.
- `earthscope-single-agent/skills/compare-earthscope-coverage` still says `load_skill(..., task=…)`, which is wrong; its test asserts that text.

## Validation and CI (clio-agent-marketplace)
- **There's no `clio-agent validate` CLI.** Use `POST /v1/agent-blueprints/validate`, or import `validate_agent_blueprint_path`.
- **Marketplace CI** (`.github/workflows/ci.yml`) runs:
  - unittest discovery over `tests/`;
  - `scripts/check_model_pins.py`;
  - `scripts/check_skill_literals.py` (the L3-leak lint, added on marketplace branch `feat/skill-literal-lint`, not merged yet; opt in per pack with `.lint-l3` + `lint-denylist.txt`; allow a line with `lint: allow-literal`);
  - real blueprint validation against clio-agent `@develop`.
- **Evals:** copy `factorio-flat/evals/` (`behavioral-cases.json`, `README.md` trace format, a grader script, fixtures). For live grinding, see the `grind-clio-case` skill.

## Names already taken
`phenotype*` (the synthetic SPOTTER workload) and `spotter*` / `SPOTTER_*` (the forensic provenance watcher). Check `ls external/clio-agent-marketplace` before naming a pack.

## Agent Plugins v1.0 (where packaging is heading)
- **The spec** ([agentplugins/agent-plugins-spec](https://github.com/agentplugins/agent-plugins-spec)) defines a plugin as `plugin.json` (`name` required) + `skills/` + optional `mcp.json`, with `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` expanded in args/env/cwd, plus client extension dirs.
- **Not in the spec** (v1.0 and the 1.1 draft): dependencies, agents/subagents, hooks, commands, UI.
- **So a clio "agent" becomes a clio-level composition of plugins**, one per level, e.g.:
  - `data-practice` (L0)
  - `phenotyping` (L1)
  - `appl-core` (L2)
  - plus generic tool plugins
- **Experts, catalogs and presets** live in clio's extension dir.
- **Because plugins can't declare interfaces,** a portable L1 plugin depends on a **shared data shape (schema + validator)**, not on tool names.
- **Build on blueprints today, and structure packs so each level could be lifted out as its own plugin.**
