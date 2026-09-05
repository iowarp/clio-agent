"""GACT agent runtime sub-package (#714 decomposition).

This package holds the agent / blueprint / expert-pack machinery carved out of
the ``clio_agent.gact.app`` monolith:

* :mod:`clio_agent.gact.agents.resolution` -- resolve agent/blueprint/expert-pack
  ``AgentDef`` rows for a session/workspace by reading ``app.state`` + disk.
* :mod:`clio_agent.gact.agents.composition` -- apply the prompt registry to a
  resolved row and render the CLIO-owned dynamic context (agent tree, tools,
  orchestrator briefing, active-workspace grounding) exposed to prompt templates.
* :mod:`clio_agent.gact.agents.runtime` -- the trajectory-retaining ``dspy.ReAct``
  subclass that drives the expert loop + ARC live-context plane.
* :mod:`clio_agent.gact.agents.builders` -- the factories that compile a registered
  dynamic agent / Agent-Blueprint expert into the concrete DSPy module that runs
  it (prompt-only, tool-declaring, and every blueprint ``module.kind``), plus the
  LM-config / tool-resolution / child-delegation machinery.

Modules here import the shared runtime base (:mod:`clio_agent.gact.runtime`) and
gact *leaves* (``gact.catalog``, ``gact.agent_blueprints``, ``gact.expert_packs``,
``gact.types``, ``gact.events``, ``gact.workflow_state``) plus stdlib / lazy
``dspy`` -- never ``gact.app`` at module load -- so the dependency graph stays
acyclic. ``resolution`` and ``composition`` co-depend (resolution applies the
prompt registry while rendering rows; composition renders a tree from resolution's
merge/child primitives); the cycle is broken by having ``composition`` reach back
to ``resolution`` through the module namespace at call time. ``builders`` depends
on ``runtime`` + ``resolution`` + ``composition``; cross-concern helpers still
owned by the ``gact.app`` turn handler / workflow-state subsystem (and the kept
``_blueprint_runner_for_agent`` / ``_run_*`` dispatch wrappers) are imported
*lazily from* ``gact.app`` inside the functions that need them -- a deliberate
strangler seam removed as those concerns are extracted.
"""

from __future__ import annotations
