"""GACT server runtime sub-package (#714 decomposition).

This package holds the cross-concern *runtime* foundation carved out of the
``clio_agent.gact.app`` monolith: the shared globals/funnel base
(:mod:`clio_agent.gact.runtime.globals`) every extracted module imports FROM, and
the leaf token/type helpers added in later steps.

It is DISTINCT from the top-level :mod:`clio_agent.runtime` package (doctor /
status / hooks). Modules here import ONLY gact *leaves*
(``gact.context``, ``gact.semantic_events``, ``gact.events``, ``gact.types``)
plus stdlib -- never ``gact.app`` -- so the dependency graph stays acyclic.
"""

from __future__ import annotations
