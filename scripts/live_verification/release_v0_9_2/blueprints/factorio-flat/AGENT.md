---
id: factorio-flat
title: Factorio Flat
display_name: Factorio Flat
version: 0.2.0
description: A scientist-facing research partner spanning research framing, evidence coordination, simulation, and adversarial review, extended with materials science, manufacturing, characterization, mechanical testing, fatigue/failure, and data analysis specialists.
root_expert: main
blueprint:
  format: agent-blueprint-v1
# Provider selection belongs to deployment configuration. Factorio Flat's
# evidence researchers use the installed web MCP, which can point at a
# self-hosted clio-search/SearXNG deployment without paid-provider credentials.
# clio-kit is provisioned once via `uv tool install clio-kit==2.10.6` (see clio-agent install/doctor).
# Installed-tool launchers replace `uvx clio-kit@...`: concurrent uvx spawns raced on a cold
# uv cache (truncated pyvenv.cfg -> dead transport -> _UnsupportedSessionAgent), and
# `uv cache prune/clean` deletes ephemeral envs under RUNNING servers (astral-sh/uv#11694).
mcp_servers:
  web: clio-kit mcp-server web
experts:
  - experts/main.md
  - experts/research_methodologist.md
  - experts/virtual_lab.md
  - experts/evidence_researcher.md
  - experts/evidence_leaf.md
  - experts/evidence_critic.md
  - experts/simulation_methodologist.md
  - experts/abaqus_engineer.md
  - experts/independent_reviewer.md
  - experts/materials_scientist.md
  - experts/manufacturing_expert.md
  - experts/characterization_expert.md
  - experts/mechanical_testing_expert.md
  - experts/fatigue_failure_expert.md
  - experts/data_analysis_expert.md
defaults:
  prompt_profile: heavy
---

# Factorio Flat

Factorio Flat keeps the scientist-facing identity and scientific integrity of
its original design while adding materials-science specialists as siblings
rather than replacements. `main` remains the root: it decides whether to
answer directly, ask a consequential clarification, load a skill itself, or
consult a specialist.

The original nine experts are unchanged in role: `research_methodologist`
(framing), `virtual_lab` (feasibility), `evidence_researcher` with its
`evidence_leaf`/`evidence_critic` children (literature evidence fan-out and
criticism), `simulation_methodologist` and `abaqus_engineer` (simulation
formulation and Abaqus/Tosca/Morphorm implementation), and
`independent_reviewer` (adversarial review).

Six materials-science specialists sit alongside them, also as direct children
of `main`, covering ground the original nine did not: `materials_scientist`
(processing-structure-property reasoning), `manufacturing_expert`
(design-to-part manufacturing route), `characterization_expert` (turning
measurements into evidence), `mechanical_testing_expert` (physical test
design and interpretation), `fatigue_failure_expert` (fatigue life prediction
and failure analysis), and `data_analysis_expert` (statistics, uncertainty,
and figures). Where a new specialist's skill set would have duplicated an
original expert's role too closely, the skill went to the original expert
instead of a new one — `simulation_methodologist` and `abaqus_engineer` carry
the FEA/topology-optimization and Abaqus/Tosca/Morphorm skills, and
`evidence_researcher` carries the literature-search/synthesis skills.

Skills are the reusable procedures each expert loads on demand; a skill can be
shared by more than one expert (for example `heat_treatment` is used by both
`materials_scientist` and `manufacturing_expert`).
