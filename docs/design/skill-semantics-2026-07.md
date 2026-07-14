# Skill-Based Semantics: Progressive Disclosure for Expert-Declared Skills

Status: LANDED S1-S4 (PRs #923/#924/#925/#926, merged to develop
2026-07-14); S5 (#921, benchmark + docs truth) in flight. Owner sign-off
2026-07-14 on the keep/remove split and declaration-only injection;
issue-tracked under umbrella #916.

Implementation refinements (recorded as landed):
- Tool-less predict/CoT experts get resolved skill BODIES compiled in (no
  tool loop to load with); react experts get metadata + load_skill (§3.2-3.3).
- The default-root auto-declaration keys on the metadata BOTH row seams carry
  (agent_blueprint_id/root_expert on the executing seam, source_blueprint on
  the listing seam).
- Skill-declared slash commands dispatch to `main` with the declared template
  COMPOSED with the skill body; workspace shadows global.
- skill.loaded events are turn-correlated and guarded (capture never fails a
  successful load); `resolved_skills` (runtime truth, per-app build cache) +
  `skill_resolution` (row-load snapshot) both ride provenance.
Date: 2026-07-14

## 0. Goal

**A declared skill is a loaded skill, provably.** An expert that declares
`skills:` sees their metadata in its prompt, loads a body on demand via
`load_skill` (the model's decision, a tool call), and every resolution and
load is visible in provenance; a missing skill is a typed diagnostic, and
nothing called "skill" masquerades as an agent. Done = the #512 acceptance
case passes: a marketplace expert's answer materially changes because it
applied its declared rubric skill, proven from provenance alone.

## 1. Problem — the `skills:` field is declared, displayed, and inert

Expert definitions (blueprint / expert-pack / user-agent frontmatter) carry a
`skills: list[str]` field (`gact/types.py` `AgentDef.skills`, parsed in
`expert_packs.py`, `user_agents.py`, `sdk/types.py`). Today that field is:

- format-validated only (an id regex — no existence check),
- projected into `capability_refs(kind="skill")` for TUI display,
- serialized into evidence and back into frontmatter,

and **nothing else**. No code resolves a declared skill id to a SKILL.md body,
no prompt-assembly path (`gact/agents/builders.py`, `composition.py`) renders
any skill context, and no `load_skill`-style tool exists. Marketplace packs
physically ship `skills/<id>/SKILL.md` bodies that experts reference
(`data-semantics`, `wildfire-smoke-impact-review`, everything under `old/`) —
all orphaned.

### 1.1 History: this worked for ~30 hours

- **#512** specified expert-declared skill loading (resolution precedence,
  runtime prompt injection, `skill_resolution`/`resolved_skills` provenance,
  missing-skill diagnostics). **PR #537** implemented it, merged to develop
  2026-06-02 (`01a37bf`).
- **`acb86c2`** ("Replace native experts with registry blueprint runtime",
  2026-06-03 — the #629 rewrite) deleted the entire implementation the next
  day: all six functions and both tests. It never came back.
- #512 remains closed-as-done. `docs/AGENT_BLUEPRINT_RUNTIME.md` still
  documents pack-local `skills/` dirs, `skills:` frontmatter, and "skill
  references are validated" as if real. The repo overstates reality (#774).

### 1.2 The second, disconnected skill concept

`gact/catalog.py::_load_skills_from_disk()` scans `~/.claude|.codex|.agents`
skill roots (and cwd equivalents) and registers **each SKILL.md as its own
tier-2 delegatable AgentDef** (`source="skill"`). This is skills-as-agents —
a Claude-Code-compat surface from the #330/#178 era. It never resolves the
`skills:` id list, and it never looks inside packs. Two things named "skill",
zero wiring between them.

## 2. How the ecosystem does it (research, 2026-07)

The **Agent Skills open standard** (Anthropic, released 2025-12-18; governed
by the Linux Foundation's Agentic AI Foundation; adopted by 30+ tools incl.
Codex CLI, Gemini CLI, VS Code/Copilot, Cursor, Goose, Kiro) defines:

- **Format**: a skill = a directory with `SKILL.md` (YAML frontmatter:
  required `name` + `description`; markdown body = the procedure) plus
  optional bundled `references/`, `scripts/`, `assets/`.
- **Progressive disclosure, three tiers**:
  1. *Metadata* (~100 tokens: name + description per skill) is compiled into
     the system prompt at session start — enough to know *when* a skill is
     relevant, nothing more.
  2. *Body*: when the model judges a skill relevant, it loads the full
     SKILL.md **itself** (a tool/file read) into context.
  3. *Bundled files*: referenced docs/scripts read or executed on demand.
- **Invocation control** (Claude Code's unified model): frontmatter
  `disable-model-invocation` (user-only, for side-effectful procedures) and
  `user-invocable` (model-only background knowledge); a skill doubles as a
  slash command.

The load decision is **the model's** — made via a tool call, never via
keyword matching in the harness. That is exactly clio's ⚑ #1 rule: the model
decides, clio executes and gates on reality (file exists, id resolves).

Full-body eager injection (what #537 did) is the known anti-pattern the
standard exists to avoid: it burns context on every turn whether or not the
skill is used (RULE 6: context is compiled, not concatenated) and collapses
on small models — the exact failure mode the marketplace grind kept hitting.

## 3. Design

### 3.1 One skill catalog, one owner module

New owner module `src/clio_agent/gact/skills.py` (no accretion into
`catalog.py`/`builders.py`): a `SkillCatalog` that discovers and resolves
skills across three scopes with deterministic precedence:

1. **pack-local** — `<active blueprint root>/skills/<id>/SKILL.md`
   (requires restoring `agent_blueprint_root` resolution from #537),
2. **workspace** — `<cwd>/.claude|.codex|.agents/skills/`,
3. **global** — `~/.claude|.codex|.agents/skills/`.

Resolution of a declared id returns a typed `ResolvedSkill` (id, title,
description, body path, scope, source, checksum) or a typed
`SkillResolutionError` (missing / ambiguous / unreadable). Existing
`_load_skills_from_disk` layouts (flat `*.md` + `**/SKILL.md`) are the
parsing substrate; discovery moves behind the catalog so there is exactly one
scanner.

Pack loader fixes: `_expert_files` (`expert_packs.py`) must exclude
`/skills/` from the loose `rglob("*.md")` scan (today a loose pack SKILL.md
is mis-parsed as an expert), and pack validation upgrades skill refs from
regex-format-only to **resolution-checked** — a missing declared skill is a
visible diagnostic on the agent row (same pattern as `prompt_resolution`),
never a silent no-op.

### 3.2 Tier 1 — metadata block in the expert prompt

At expert build (`builders.py`, where `agent_prompt + workspace_context +
child_context` are joined), an expert that declares `skills:` gets one more
compiled block:

```
## Skills available to you
Load a skill BEFORE performing the task it covers — it contains the
procedure/rubric you are expected to follow.
- <id>: <description from frontmatter>   (per declared skill)
Load with the load_skill tool.
```

Names + descriptions only (~100 tokens/skill), never bodies. Rendered from
`ResolvedSkill` metadata; unresolved ids are omitted from the block and
surfaced as diagnostics instead.

### 3.3 Tier 2 — the `load_skill` tool (model-invoked)

Experts that declare skills automatically get a `load_skill(skill_id)` tool,
the same way orchestrators get generated child-delegation tools — runtime
infrastructure, not part of the 5–7 curated domain-tool budget (RULE 5).

- Returns the SKILL.md body plus a listing of bundled files (tier 3: the
  expert's existing fs tools read those, subject to file policy; pack skill
  dirs join the readable roots for the session that activated the pack).
- The body enters context as a normal tool **observation** → it lands in the
  ARC live plane as segments, is compactable, and survives the working-set
  fold like any other observation. No special context plumbing.
- Unknown id → typed tool error naming the declared ids (no fuzzy matching).

### 3.4 Provenance + no-silent-fallback

- Restore the #512 vocabulary: `skill_resolution` (per declared id: status
  resolved/missing/ambiguous + path + scope) in agent runtime metadata and
  evidence; `resolved_skills` on turn provenance.
- `skill.loaded` semantic event on the highway when the tool fires (id,
  scope, source path, size) — TUI-renderable, benchmark-provable.
- Every degradation (missing skill, unreadable body, ambiguous resolution)
  emits a structured reason that reaches the trace/API, per the cleanup
  ground rules.

### 3.5 Skills-as-agents (concept B) — REMOVE (owner decision 2026-07-14)

A skill is procedural knowledge, not an actor. The `source="skill"` AgentDef
registration is removed hard, no compat flag: no skill rows in the agent
tree/registry, no skill ids as delegation targets (`resolution.py:147`), and
the self-registration hack (`resolution.py:472-486`) dies with it. The
mechanism was concretely harmful: a skill-agent is a prompt-only tier-2
expert with no tools, so delegating to one produces the
fabricate-from-prior-knowledge answer the orchestrator briefing exists to
prevent, and every SKILL.md on the user's disk grew the routing space all
models see.

What survives, re-fed from `SkillCatalog`:

- **Slash commands** (#330 semantics): `runtime/commands.py::user_command_rows`
  derives command rows from `ResolvedSkill`s instead of skill-AgentDefs;
  wire shape out of the routes unchanged.
- The `capability_refs(kind="skill")` display projection.

A skill id used as a delegation/agent target after the cut raises a typed
resolution error ("skills are not delegatable; declare it under `skills:` on
the expert, or invoke `/<id>`"), never a silent skip — with sabotage twins
proving a SKILL.md on disk no longer materializes an agent row.

### 3.6 Declaration-only injection (owner decision 2026-07-14)

An expert sees only the skills it declares (curated surface, not
Claude-Code-style ambient visibility). The default/chat expert (#694/#752)
auto-declares workspace skills so user-authored skills still work in plain
chat.

## 4. What we explicitly do NOT do

- No eager full-body injection (the #537 approach) — RULE 6.
- No keyword/heuristic auto-loading of skills by the harness — ⚑ #1; the
  model invokes `load_skill` or it doesn't.
- No fifth store — skills live on disk (pack/workspace/global), resolution is
  computed, loads are ARC observations (RULE 4).
- No new frontmatter dialect — the open-standard SKILL.md shape
  (`name`/`description` + body) as already parsed.

## 5. Slices (umbrella #916)

- **#917 — S1: SkillCatalog + resolution + diagnostics.** Owner module,
  three-scope precedence, pack scanner exclusion, resolution-checked pack
  validation, typed missing-skill diagnostics. Tests: precedence (pack
  shadows workspace shadows global), missing/ambiguous/unreadable,
  loose-pack SKILL.md no longer parsed as an expert.
- **#918 — S2: remove skills-as-agents.** No AgentDef from SKILL.md;
  slash-command derivation rewired to `SkillCatalog` (wire shape stable);
  typed delegation error; sabotage twins per §3.5.
- **#919 — S3: progressive disclosure runtime.** Builders render the tier-1
  block; `load_skill` auto-attached; body flows as observation into ARC;
  default-expert workspace auto-declaration. Tests: block present iff skills
  declared; body NOT in the built system prompt (sabotage twin: assert
  absence pre-load); tool returns body + provenance; unknown-id typed error;
  observation lands as segments.
- **#920 — S4: provenance + events + TUI.** `skill_resolution` /
  `resolved_skills` in evidence, `skill.loaded` semantic event, gact-tui
  renders the load row (companion: gact-tui#315).
- **#921 — S5: benchmark + docs truth (campaign-done gate).** The #512
  acceptance case: a marketplace pack expert whose declared rubric skill
  materially changes its answer, proven via provenance in a real session
  (fixtures already shipped: `data-semantics`,
  `wildfire-smoke-impact-review`). Docs pass: `AGENT_BLUEPRINT_RUNTIME.md`
  updated to describe what IS (ties to #774).

## 6. Issue map

- **#916** (open) — the umbrella for this design; supersedes #512.
- **#512** (closed) — the original spec; closed by #537 but the
  implementation was deleted by `acb86c2` the next day (supersession
  recorded on the issue).
- **#774** (open) — repo/meta-doc truth: `AGENT_BLUEPRINT_RUNTIME.md`
  currently documents unimplemented skill semantics (fixed by #921).
- **#628** (open) — the benchmark lane the #921 case belongs to.
- **#647** (open) — blueprint prompt semantics for parent/child handoff;
  adjacent prompt-composition work, coordinate to avoid collision in
  `builders.py`/`composition.py`.
- **#694 / #752** (open) — default ReAct chat expert / neutral baseline
  blueprint; first consumers via the workspace auto-declaration (#919).
- **gact-tui #315** (open) — companion: render `skill.loaded` + resolution
  status; skills leave the agents tree.
- **gact-tui #215** (open) — command-palette / slash-command semantics;
  skills-as-commands surface must stay coherent with it.
- Marketplace: packs already ship SKILL.md fixtures; closed #27/#28 added
  them — they become live the moment #919 lands.
