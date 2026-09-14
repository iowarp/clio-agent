# Factorio Flat design note

Factorio Flat follows this repository's pack-local README convention and records
design rationale, not copied prompt text.

- OpenAI describes an agent as instructions plus tools and recommends separate
  agents when independent work has complex instructions or tool surfaces, while
  keeping multi-agent use selective. Factorio Flat therefore keeps a small root
  identity, explicit tool ownership, and specialist-specific prompts.
  [OpenAI, Building agents](https://developers.openai.com/tracks/building-agents#orchestration)
- Anthropic distinguishes open-ended agents from fixed workflows, recommends
  parallel work for independent concerns, and uses orchestrator-workers when the
  subtasks must be chosen dynamically. Factorio Flat puts those decisions in an
  on-demand coordination skill instead of an always-on sequence.
  [Anthropic, Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- Google recommends direct, structured instructions, decomposition of complex
  prompts, and aggregation of independent tasks. Factorio Flat uses small skill
  entrypoints with bundled references for progressive disclosure.
  [Google, Gemini prompt design strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies)

Behavior is assessed outside the prompt through normalized public responses and
tool traces. The eval cases test semantic routing, clarification quality,
parallelism, and task continuity without matching exact prose.

## Runtime contract

`ask_user` and `create_a2ui_surface` are explicit runtime tool dependencies for
the experts that declare them. The runtime must expose those names during pack
validation and execution, including on child experts. `create_artifact` remains
an auto-attached ReAct lifecycle tool, so a skill can use it for durable
deliverables without adding it to curated expert allowlists.

## Structure

Experts decide what kind of scientific reasoning a question needs; skills carry
the specialized knowledge and procedures to perform it. A skill can be shared by
more than one expert (for example `heat_treatment` is used by both
`materials_scientist` and `manufacturing_expert`).

`experts/` holds a single flat layer: `main` is the root (`tier: 1`, no
`parent:`) and scientist-facing identity. Fourteen experts sit at `tier: 2`
with `parent: main`, except `evidence_leaf` and `evidence_critic`, which are
`tier: 3` under `parent: evidence_researcher` — the pack's one two-level
fan-out, unchanged from the original design:

- `research_methodologist` — research framing and study logic
- `virtual_lab` — resource and feasibility mapping
- `evidence_researcher` (→ `evidence_leaf`, `evidence_critic`) — literature
  evidence fan-out, fetching, and independent criticism
- `simulation_methodologist` — FEA and topology-optimization formulation
- `abaqus_engineer` — Abaqus/Tosca/Morphorm implementation
- `independent_reviewer` — adversarial review of the full package
- `materials_scientist` — processing-structure-property reasoning
- `manufacturing_expert` — design-to-part manufacturing route
- `characterization_expert` — turning measurements into evidence
- `mechanical_testing_expert` — physical test design and interpretation
- `fatigue_failure_expert` — fatigue life prediction and failure analysis
- `data_analysis_expert` — statistics, uncertainty, and figures

The six materials-science specialists were added as siblings, not
replacements: nothing about the original nine's roles changed. Where a
materials-science specialist would have duplicated an original expert's role
too closely, its skills went to the original expert instead of a new one —
`simulation_methodologist`/`abaqus_engineer` carry the FEA/topology-optimization
and Abaqus/Tosca/Morphorm skills, and `evidence_researcher` carries the
literature-search/synthesis skills, rather than a separate
`simulation_modeling_expert` or `literature_researcher` existing alongside
them.

`skills/` is a single flat layer, matching `experts/` — one `skills/<name>/SKILL.md`
per skill, no category subdirectories. It holds two generations side by side:
the original six coordination/formulation skills (`coordinate-scientific-work`,
`frame-research-problem`, `maintain-scientific-dossier`, `formulate-abaqus-package`,
`audit-scientific-package`, all owned by `main`; `evidence-fanout`, owned by
`evidence_researcher`), unchanged from the original design, and the 55
materials-science skills added alongside them. The materials-science skills
fall into eight informal groups by activity (research, materials,
manufacturing, simulation, testing, characterization, fatigue/failure, data),
but that grouping exists only in which experts load which skills, not in the
directory layout. This is a first-pass, intentionally granular roster rather
than a merged/trimmed one — expect it to be pruned and consolidated as it sees
use.
