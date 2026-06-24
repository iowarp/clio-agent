"""GACT agent runtime sub-package (#714 decomposition).

This package holds the agent / blueprint / expert-pack machinery carved out of
the ``clio_agent.gact.app`` monolith. The first modules landed here are the
stateless *resolution* and prompt *composition* helpers:

* :mod:`clio_agent.gact.agents.resolution` -- resolve agent/blueprint/expert-pack
  ``AgentDef`` rows for a session/workspace by reading ``app.state`` + disk.
* :mod:`clio_agent.gact.agents.composition` -- apply the prompt registry to a
  resolved row and render the CLIO-owned dynamic context (agent tree, tools,
  orchestrator briefing, active-workspace grounding) exposed to prompt templates.

Modules here import the shared runtime base (:mod:`clio_agent.gact.runtime`) and
gact *leaves* (``gact.catalog``, ``gact.agent_blueprints``, ``gact.expert_packs``,
``gact.types``) plus stdlib -- never ``gact.app`` -- so the dependency graph stays
acyclic. ``resolution`` and ``composition`` co-depend (resolution applies the
prompt registry while rendering rows; composition renders a tree from resolution's
merge/child primitives); the cycle is broken by having ``composition`` reach back
to ``resolution`` through the module namespace at call time.
"""

from __future__ import annotations
