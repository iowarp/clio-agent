---
name: author-agent-pack
description: >-
  Design and write a clio-agent marketplace pack (Agent Blueprint today, Agent
  Plugins v1.0 later) that genuinely generalizes: pick the level it is honestly
  built at (L0 capability / L1 domain / L2 source / L3 instance), keep lower-level
  facts out of the pack, put knowledge in skills rather than format-specific MCPs,
  use only generic UI, and prove the claim with held-out evals and the
  skill-literal lint. Use when asked to create, restructure, or review a clio
  agent pack, blueprint, plugin, expert, or pack skill, or when a new dataset or
  facility is to be supported by an agent.
---

# Author a clio agent pack that actually generalizes

This skill records the lessons from the EarthScope/NDP pack history and from the OPAL/APPL
plant-phenotyping design (Sept 2026).

It is a companion to `grind-clio-case`:
- **this skill:** *what* to build, and at *which level*;
- **`grind-clio-case`:** how to grind a case to acceptance with live traces.

Reference files, loaded as needed:
- `reference/levels.md`: the level model, the definition and tests, and the history of the failure.
- `reference/pack-anatomy.md`: blueprint, expert and skill formats as the code implements them today, and the move to Agent Plugins.
- `reference/data-agents.md`: how to build agents that onboard unseen datasets. Covers the lessons-learned skill, experiment cards and saved loaders.
- `reference/ui-and-tools.md`: generic UI rules, what the A2UI kernel can and can't do, MCP/tool guidance and shell limits.
- `reference/checklist.md`: the review checklist to run before calling a pack done.

---

## The one failure this skill exists to prevent

> **We build agents that claim to be one level but hardcode the level below, and find out only
> when we try to generalize.**

- **EarthScope (in this marketplace):**
  - `earthscope-gnss-region` (the first version) had run-specific station IDs and file names in its expert prompts (`P475`, `P473`, `MTA1.CI.LY_.30.csv`, …). That's an **L3 leak**.
  - `earthscope-single-agent` (the final version) removed those. It still bakes in **L2**: GNSS-only skills, a GNSS `workflow_state`, and an `earthscope-stations` UI catalog, while describing itself as an "NDP scientist".
- **ORNL's OPAL agent:** its `knowledge_base.yaml` mixes experiment counts ("1,710 of 15,096 rows"), `default_trait_modality: rgb2` and the nickel treatment into what should be generic prompts.

**Rule of thumb:**
- **Name** a pack for the highest level it has been **tested** at, with **two or more instances**.
- **Claiming a middle level honestly is fine.** The sin is a name above what the implementation supports.

## Workflow

1. **Name the level first.** Decide which level the pack is (usually L2: one source, one format, one facility) and state its **contract**: the format id, the version range, and what "any instance" means. Write it in the `AGENT.md` body.
2. **List the instances you'll test on:** at least two real ones, plus synthetic spec-conforming variants and out-of-contract cases that must be refused.
   - *No second instance available?* **Mutate the one you have**: recode factors, unbalance it, drop a source, rename columns, bump the version.
3. **Put each piece of knowledge at its level** (see `reference/levels.md`):
   - **L0 habits** → general skills.
   - **L1 domain methods** → domain skills, tagged `level:L1`.
   - **L2 format/instrument facts** → source skills.
   - **L3 facts** → **never in the pack**. They're discovered at runtime, asked from humans, or recorded in an experiment card stored with the data.
4. **Prefer skills plus generic tools over a format-specific MCP.**
   - MCPs expose **verbs**, never **semantics**.
   - Let the agent write its own loader and save it with the data (`reference/data-agents.md`).
   - Deterministic helpers go in **skill-bundled scripts** (editable, versioned), not in closed tools.
5. **UI is L0 only.** Use or extend core components (`clio.mesh-viewport.v1`, charts, tables, maps). Packs contribute **presets/aliases only**. No `<domain>-stations`-style catalogs.
6. **Guard it:**
   - add `.lint-l3` + `lint-denylist.txt` to the pack (the marketplace `scripts/check_skill_literals.py`);
   - write evals that swap the instance (L3 substitution) and, where possible, the source (L2 substitution);
   - give the second session a test that it **reuses** what the first one learned.
7. **Review with `reference/checklist.md`** before declaring it done, and get a critical second opinion from a separate reviewer agent.

## Non-negotiables

- **No instance literals** in `AGENT.md`, experts or skills: IDs, dates, counts, measured magnitudes ("~40× too small"), concrete dataset file names. Examples must be generic placeholders.
- **Lessons are checks, not facts.**
  - Allowed: "check whether flags agree with the data".
  - Not allowed: "in dataset X the flag covers 270 of 4,690 rows". That belongs in the experiment card.
- **Promotion needs evidence.**
  - A check moves from an experiment card into an L2 skill only after it's seen in **two or more instances**, or stated in the format's own docs.
  - General habits (sentinel detection, empty/near-duplicate columns, flag–data agreement) can go straight into L0.
- **Asking isn't failing; hardcoding is.**
  - A general agent may need per-instance facts (what does a treatment code mean? units?).
  - It gets them at runtime: discover, ask, record in the card.
  - It never ships them.
- **Refuse out-of-contract input loudly** (e.g. an unsupported `export_version`). Never half-handle it.
- **Branches:** in this repo family, do feature work on branches off `develop` (or `main` where no `develop` exists), in separate worktrees, and don't merge until the owner says so.
