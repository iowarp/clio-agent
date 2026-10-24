"""GACT agent runtime sub-package (#714 decomposition).

This package builds and runs the session's agent from its pinned plugin
resolution (``docs/PLUGINS_AND_FLOWS.md``), carved out of the
``clio_agent.gact.app`` monolith:

* :mod:`clio_agent.gact.agents.resolution` -- which agent a session runs: its
  pinned :class:`~clio_agent.plugins.resolve.Resolution`, the executable
  ``AgentDef`` of a resolved agent or defined subagent, and its server set.
* :mod:`clio_agent.gact.agents.composition` -- the per-session runtime context
  (the bound workspace and its write roots) appended to the composed prompt.
* :mod:`clio_agent.gact.agents.clio_react` -- ``ClioReAct``, clio's agent loop
  (a ``dspy.Module``) that drives the ARC live-context plane; the ``react`` flow
  runs it.
* :mod:`clio_agent.gact.agents.builders` -- the factory that builds a resolved
  agent's DSPy module (its plugins' built-in, Python and MCP tools, skills and
  spawn tools), plus the LM-config / tool-resolution machinery.

Modules here import the shared runtime base (:mod:`clio_agent.gact.runtime`),
``clio_agent.plugins`` and gact *leaves* (``gact.catalog``, ``gact.types``,
``gact.events``, ``gact.workflow_state``) plus stdlib / lazy ``dspy`` -- never
``gact.app`` at module load -- so the dependency graph stays acyclic.
``builders`` depends on ``runtime`` + ``resolution`` + ``composition``;
cross-concern helpers still owned by the ``gact.app`` turn handler are imported
*lazily from* ``gact.app`` inside the functions that need them -- a deliberate
strangler seam removed as those concerns are extracted.
"""

from __future__ import annotations
